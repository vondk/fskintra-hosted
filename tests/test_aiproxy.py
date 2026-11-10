import json
import os
import threading

import anthropic
import httpx2
import pytest

from app import aiproxy, db, jobs, tenants
from conftest import HOST

OWN_KEY = 'sk-ant-egen-noegle-ok'
BODY = {'model': 'claude-haiku-5-5', 'max_tokens': 100,
        'system': 'Du hjælper en forælder',
        'messages': [{'role': 'user',
                      'content': 'Ugeplan: Ugeplan for 4B - uge 45'}]}


def _message(text='{"punkter": []}', stop_reason='end_turn'):
    return json.dumps({
        'id': 'msg_1', 'type': 'message', 'role': 'assistant',
        'model': 'claude-haiku-5-5', 'stop_reason': stop_reason,
        'stop_sequence': None,
        'content': [{'type': 'text', 'text': text}],
        'usage': {'input_tokens': 10, 'output_tokens': 5},
    }).encode()


class Upstream:
    '''Erstatning for Claude API - husker hvilken nøgle hvert kald brugte'''

    def __init__(self):
        self.calls = []
        self.status = 200
        self.body = _message()
        self.error = None

    def __call__(self, query, headers, raw):
        headers = dict(headers)
        key = headers.pop('x-api-key', None) or \
            headers.pop('authorization').removeprefix('Bearer ')
        self.calls.append((key, query, headers, json.loads(raw)))
        if self.error:
            raise self.error
        return self.status, {'content-type': 'application/json',
                             'request-id': 'req_1'}, self.body


@pytest.fixture
def upstream(ctx):
    up = Upstream()
    ctx.ai_proxy = aiproxy.AiProxy(ctx.db, send=up)
    ctx.ai_proxy.url = 'http://127.0.0.1:1'   # "kører" uden en rigtig port
    return up


def _post(ctx, token, body=BODY, **headers):
    return ctx.ai_proxy.app.test_client().post(
        '/v1/messages?beta=true', json=body,
        headers=dict({'x-api-key': token}, **headers))


def test_same_request_from_two_parents_costs_one_call(ctx, upstream):
    with ctx.ai_proxy.session('nøgle-a') as a, \
            ctx.ai_proxy.session('nøgle-b') as b:
        assert a != b and 'nøgle' not in a
        first = _post(ctx, a)
        second = _post(ctx, b)
    assert first.status_code == second.status_code == 200
    assert first.data == second.data == upstream.body
    # Kun den første forælders nøgle blev brugt - og sendt videre uændret
    (key, query, headers, body), = upstream.calls
    assert key == 'nøgle-a' and query == {'beta': 'true'} and body == BODY


def test_other_model_or_text_is_a_miss(ctx, upstream):
    with ctx.ai_proxy.session('nøgle') as t:
        _post(ctx, t)
        _post(ctx, t, dict(BODY, model='claude-opus-5-5'))
        _post(ctx, t, dict(BODY, system='En anden prompt'))
        _post(ctx, t, **{'anthropic-beta': 'server-side-fallback-2026-07-01'})
        _post(ctx, t)
    assert len(upstream.calls) == 4


def test_token_only_works_during_the_run(ctx, upstream):
    assert _post(ctx, 'proxy-gaet').status_code == 401
    assert _post(ctx, '').status_code == 401
    with ctx.ai_proxy.session('nøgle') as t:
        assert _post(ctx, t).status_code == 200
        # Anthropics klient sender tokenet som Bearer
        r = ctx.ai_proxy.app.test_client().post(
            '/v1/messages', json=BODY,
            headers={'Authorization': 'Bearer ' + t})
        assert r.status_code == 200
    # Heller ikke et svar fra cachen uden et gyldigt token
    assert _post(ctx, t).status_code == 401
    assert len(upstream.calls) == 1


def test_session_without_running_proxy_or_key_gives_no_token(ctx, upstream):
    with ctx.ai_proxy.session('') as t:
        assert t is None
    ctx.ai_proxy.url = None
    with ctx.ai_proxy.session('nøgle') as t:
        assert t is None


@pytest.mark.parametrize('status,body', [
    (529, b'{"type": "error", "error": {"type": "overloaded_error"}}'),
    (200, _message(stop_reason='max_tokens')),
    (200, _message(stop_reason='refusal')),
    (200, b'ikke json'),
])
def test_failures_are_passed_on_but_not_cached(ctx, upstream, status, body):
    upstream.status, upstream.body = status, body
    with ctx.ai_proxy.session('nøgle') as t:
        r = _post(ctx, t)
        assert (r.status_code, r.data) == (status, body)
        assert r.headers['request-id'] == 'req_1'
        _post(ctx, t)
    assert len(upstream.calls) == 2


def test_unreachable_api_gives_502(ctx, upstream):
    upstream.error = httpx2.ConnectError('nede')
    with ctx.ai_proxy.session('nøgle') as t:
        r = _post(ctx, t)
    assert r.status_code == 502 and r.json['error']['type'] == 'api_error'


def test_bad_requests_never_reach_the_api(ctx, upstream):
    client = ctx.ai_proxy.app.test_client()
    with ctx.ai_proxy.session('nøgle') as t:
        h = {'x-api-key': t}
        assert _post(ctx, t, dict(BODY, stream=True)).status_code == 400
        assert client.post('/v1/messages', data='x', headers=h
                           ).status_code == 400
        assert client.post('/v1/messages', json=[1], headers=h
                           ).status_code == 400
        big = 'x' * (aiproxy.MAX_BODY + 1)
        assert client.post('/v1/messages', data=big, headers=h
                           ).status_code == 413
        assert client.get('/v1/messages', headers=h).status_code == 405
    assert upstream.calls == []


def test_old_answers_expire(ctx, upstream):
    conn = ctx.db()
    with ctx.ai_proxy.session('nøgle') as t:
        _post(ctx, t)
        conn.execute('UPDATE ai_cache SET created_at = ?',
                     (db.now() - aiproxy.TTL - 60,))
        _post(ctx, t)                       # for gammelt: hentes igen
        assert len(upstream.calls) == 2
        conn.execute('UPDATE ai_cache SET created_at = ?',
                     (db.now() - aiproxy.TTL - 60,))
        aiproxy.prune(conn)
        assert conn.execute('SELECT COUNT(*) FROM ai_cache'
                            ).fetchone()[0] == 0
        _post(ctx, t)
        aiproxy.prune(conn)                 # friske svar bliver stående
        assert conn.execute('SELECT COUNT(*) FROM ai_cache'
                            ).fetchone()[0] == 1


def test_simultaneous_requests_share_one_call(ctx, upstream):
    with ctx.ai_proxy.session('nøgle') as t:
        threads = [threading.Thread(target=_post, args=(ctx, t))
                   for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert len(upstream.calls) == 1


def _tenant(ctx, email, ai_mode, ai_key=None):
    conn = ctx.db()
    tid = tenants.create(conn, email)
    tenants.update(conn, tid, hostname=HOST, username='petjen',
                   password_enc=ctx.crypto.encrypt('Kode-123', tid,
                                                   'password'),
                   ai_mode=ai_mode, status='active', needs_catchup=0,
                   ai_key_enc=ctx.crypto.encrypt(ai_key, tid, 'ai_key')
                   if ai_key else None,
                   ai_model='claude-opus-5-5' if ai_key else None)
    return tid


def _env_of(ctx, tid):
    path = os.path.join(ctx.settings.tenants_dir, tid, 'env.json')
    with open(path) as f:
        return json.load(f)['env']


def test_run_gets_a_token_instead_of_the_key(ctx, env, upstream):
    shared = _tenant(ctx, 'mor@example.net', 'shared')
    own = _tenant(ctx, 'far@example.net', 'own', OWN_KEY)
    off = _tenant(ctx, 'moster@example.net', 'off')
    for tid in (shared, own, off):
        assert jobs.execute(ctx, tid, 'run').exit_code == 0

    seen = _env_of(ctx, shared)
    token = seen['FSKINTRA_ANTHROPIC_API_KEY']
    assert token.startswith('proxy-')
    assert seen['FSKINTRA_AI_BASE_URL'] == ctx.ai_proxy.url
    assert seen['FSKINTRA_AI_MODEL'] == 'claude-haiku-5-5'
    assert env['FSKINTRA_ANTHROPIC_API_KEY'] not in json.dumps(seen)
    # Tokenet er dødt, når kørslen er slut
    assert _post(ctx, token).status_code == 401

    seen = _env_of(ctx, own)
    assert seen['FSKINTRA_ANTHROPIC_API_KEY'].startswith('proxy-')
    assert seen['FSKINTRA_AI_MODEL'] == 'claude-opus-5-5'
    assert OWN_KEY not in json.dumps(seen)

    seen = _env_of(ctx, off)
    assert 'FSKINTRA_ANTHROPIC_API_KEY' not in seen
    assert 'FSKINTRA_AI_BASE_URL' not in seen


def test_run_without_proxy_gets_the_key_directly(ctx, env):
    assert ctx.ai_proxy.url is None           # ikke startet
    tid = _tenant(ctx, 'mor@example.net', 'shared')
    assert jobs.execute(ctx, tid, 'run').exit_code == 0
    seen = _env_of(ctx, tid)
    assert seen['FSKINTRA_ANTHROPIC_API_KEY'] == \
        env['FSKINTRA_ANTHROPIC_API_KEY']
    assert 'FSKINTRA_AI_BASE_URL' not in seen


def test_real_sdk_through_a_real_server(ctx, monkeypatch):
    '''Hele vejen: Anthropics klient -> proxyen på 127.0.0.1 -> "API'et"'''
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy',
                 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    up = Upstream()
    proxy = aiproxy.AiProxy(ctx.db, send=up)
    server = proxy.start(0)
    try:
        assert proxy.url.startswith('http://127.0.0.1:')
        replies = []
        for key in ('nøgle-a', 'nøgle-b'):
            with proxy.session(key) as token:
                client = anthropic.Anthropic(api_key=token,
                                             base_url=proxy.url,
                                             max_retries=0, timeout=10)
                replies.append(client.beta.messages.create(
                    betas=['server-side-fallback-2026-07-01'], **BODY))
        assert [r.content[0].text for r in replies] == ['{"punkter": []}'] * 2
        assert replies[0].stop_reason == 'end_turn'
        (key, query, headers, body), = up.calls
        assert key == 'nøgle-a'
        assert headers['anthropic-beta'] == 'server-side-fallback-2026-07-01'
        assert 'anthropic-version' in headers

        with pytest.raises(anthropic.AuthenticationError):
            anthropic.Anthropic(api_key='proxy-gaet', base_url=proxy.url,
                                max_retries=0).beta.messages.create(**BODY)
    finally:
        server.shutdown()

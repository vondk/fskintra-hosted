import dataclasses
import json
import os
import re
import threading

import pytest

from app import auth, db, settings as settings_mod, tenants
from app.context import Context
from app.web import create_public_app
from conftest import (INVITE, PUBLIC, FakeMailer, csrf, make_client,
                      new_client, post, save_settings, signup)


# Security headers og CSRF

@pytest.mark.parametrize('path', ['/', '/healthz', '/privatliv', '/findes'])
def test_security_headers_on_every_response(client, path):
    r = client.get(path)
    h = r.headers
    assert "default-src 'none'" in h['Content-Security-Policy']
    assert "frame-ancestors 'none'" in h['Content-Security-Policy']
    assert h['X-Frame-Options'] == 'DENY'
    assert h['X-Content-Type-Options'] == 'nosniff'
    assert h['Referrer-Policy'] == 'same-origin'
    assert h['Strict-Transport-Security'].startswith('max-age=')
    assert h['Cache-Control'] == 'no-store'


def test_csrf_cookie_flags(client):
    r = client.get('/')
    cookie = [c for c in r.headers.getlist('Set-Cookie')
              if c.startswith('__Host-csrf=')][0]
    assert 'Secure' in cookie and 'HttpOnly' in cookie
    assert 'SameSite=Lax' in cookie and 'Path=/' in cookie


def test_post_without_csrf_is_rejected(client, ctx):
    client.get('/')
    r = client.post('/signup', data={'email': 'a@example.net',
                                     'invite': INVITE})
    assert r.status_code == 400 and not ctx.mailer.sent


def test_meta_referrer_does_not_force_origin_null(client):
    # <meta name="referrer" content="no-referrer"> ville få browsere til at
    # sende Origin: null på alle formularer
    assert b'content="same-origin"' in client.get('/').data
    assert b'no-referrer' not in client.get('/').data


def test_origin_null_with_valid_token_is_accepted(client, ctx):
    r = client.post('/signup', data={'email': 'a@example.net',
                                     'invite': INVITE,
                                     'csrf': csrf(client)},
                    headers={'Origin': 'null'})
    assert r.status_code == 200 and len(ctx.mailer.sent) == 1


def test_origin_null_without_token_is_rejected(client, ctx):
    client.get('/')
    r = client.post('/signup', data={'email': 'a@example.net',
                                     'invite': INVITE},
                    headers={'Origin': 'null'})
    assert r.status_code == 400 and not ctx.mailer.sent


def test_same_origin_post_is_accepted(client, ctx):
    r = client.post('/signup', data={'email': 'a@example.net',
                                     'invite': INVITE,
                                     'csrf': csrf(client)},
                    headers={'Origin': PUBLIC})
    assert r.status_code == 200 and len(ctx.mailer.sent) == 1


def test_cross_origin_post_is_rejected(client, ctx):
    r = client.post('/signup', data={'email': 'a@example.net',
                                     'invite': INVITE,
                                     'csrf': csrf(client)},
                    headers={'Origin': 'https://ond.example'})
    assert r.status_code == 400 and not ctx.mailer.sent


@pytest.mark.parametrize('method,path', [
    ('get', '/overblik'), ('get', '/status'), ('get', '/settings'),
    ('get', '/export'),
    ('post', '/delete'), ('post', '/settings'), ('post', '/run-now'),
    ('post', '/recipients/add'), ('post', '/kalender/nyt'),
    ('post', '/kalender/slet'), ('post', '/opdater')])
def test_protected_routes_require_login(client, method, path):
    if method == 'get':
        r = client.get(path)
    else:
        r = post(client, path)
    assert r.status_code == 302 and r.headers['Location'].endswith('/')


@pytest.mark.parametrize('path', ['/admin', '/admin/login'])
def test_admin_routes_do_not_exist_on_public_app(client, path):
    assert client.get(path).status_code == 404


# Tilmelding og magic links

def test_wrong_invite_sends_nothing(client, ctx):
    r = post(client, '/signup', {'email': 'a@example.net', 'invite': 'gæt'})
    assert r.status_code == 400 and not ctx.mailer.sent


def test_signup_link_flow(client, ctx):
    post(client, '/signup', {'email': 'A@Example.net', 'invite': INVITE})
    (to, _subject, body), = ctx.mailer.sent
    assert to == 'a@example.net' and PUBLIC + '/auth/' in body
    token = ctx.mailer.last_link()

    # GET må ikke forbruge linket (mail-scannere)
    assert client.get('/auth/' + token).status_code == 200
    assert client.get('/auth/' + token).status_code == 200

    r = post(client, '/auth/' + token)
    assert r.status_code == 303 and r.headers['Location'].endswith(
        '/settings')
    sid = client.get_cookie('__Host-sid', domain='hosted.example')
    assert sid is not None
    # Kun en hash af sessionen gemmes
    row = ctx.db().execute('SELECT hash FROM sessions').fetchone()
    assert row['hash'] != sid.value and len(row['hash']) == 64

    # Engangs: anden brug fejler
    assert post(client, '/auth/' + token).status_code == 404
    assert client.get('/status').status_code == 200


def test_expired_token_is_rejected(client, ctx):
    post(client, '/signup', {'email': 'a@example.net', 'invite': INVITE})
    token = ctx.mailer.last_link()
    ctx.db().execute('UPDATE tokens SET expires_at = 1')
    assert client.get('/auth/' + token).status_code == 404
    assert post(client, '/auth/' + token).status_code == 404


def test_login_does_not_reveal_unknown_addresses(client, ctx):
    r = post(client, '/login', {'email': 'ukendt@example.net'})
    assert r.status_code == 200 and not ctx.mailer.sent
    signup(new_client(client), ctx, 'kendt@example.net')
    n = len(ctx.mailer.sent)
    r2 = post(client, '/login', {'email': 'kendt@example.net'})
    assert r2.status_code == 200 and len(ctx.mailer.sent) == n + 1
    assert r.data == r2.data


def test_login_answers_without_waiting_for_the_mail_server(env):
    '''Mailen sendes i baggrunden: svaret kommer med det samme (ingen
    ventetid at trykke igen i, og ingen svartid der afslører adressen)'''
    release = threading.Event()

    class SlowMailer(FakeMailer):
        def send(self, *a, **kw):
            assert release.wait(5)
            return super().send(*a, **kw)

    db.close_all()
    ctx = Context(settings_mod.load(), mailer=SlowMailer())
    try:
        tenants.create(ctx.db(), 'foraelder@example.net')
        client = make_client(create_public_app(ctx))
        r = post(client, '/login', {'email': 'foraelder@example.net'})
        assert r.status_code == 200 and b'name="code"' in r.data
        assert ctx.mailer.sent == []
        release.set()
        ctx._mail_pool.shutdown(wait=True)
        assert [m[0] for m in ctx.mailer.sent] == ['foraelder@example.net']
        assert re.fullmatch(r'\d{6}', ctx.mailer.last_code())
    finally:
        release.set()
        db.close_all()


def test_signup_is_rate_limited(client, ctx):
    for _ in range(3):
        post(client, '/signup', {'email': 'a@example.net', 'invite': INVITE})
    r = post(client, '/signup', {'email': 'a@example.net', 'invite': INVITE})
    assert r.status_code == 429 and len(ctx.mailer.sent) == 3


# Indstillinger

def _db_bytes(ctx):
    conn = ctx.db()
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    data = b''
    for suffix in ('', '-wal'):
        path = ctx.settings.db_path + suffix
        if os.path.exists(path):
            data += open(path, 'rb').read()
    return data


def test_settings_save_encrypts_password(client, ctx):
    tid = signup(client, ctx)
    r = save_settings(client)
    assert r.status_code == 303
    t = tenants.get(ctx.db(), tid)
    assert t['status'] == 'active' and t['needs_catchup'] == 1
    assert json.loads(t['children']) == ['Anna 3B']
    assert t['password_enc'].startswith('v1.')
    assert b'Rigtig-Kode-123' not in _db_bytes(ctx)
    assert tenants.decrypt_secrets(ctx.crypto, t)[0] == 'Rigtig-Kode-123'


def test_rejected_login_saves_nothing(client, ctx):
    tid = signup(client, ctx)
    r = save_settings(client, username='forkert')
    assert r.status_code == 400 and 'afviste'.encode() in r.data
    t = tenants.get(ctx.db(), tid)
    assert t['password_enc'] is None and t['status'] == 'new'


def test_school_must_be_in_allowlist(client, ctx):
    tid = signup(client, ctx)
    r = save_settings(client, hostname='169.254.169.254')
    assert r.status_code == 400
    assert tenants.get(ctx.db(), tid)['hostname'] is None
    assert not tenants.runs(ctx.db(), tid)  # intet login forsøgt


def test_password_kept_when_left_blank(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    r = save_settings(client, password='', section_doc='on')
    assert r.status_code == 303
    t = tenants.get(ctx.db(), tid)
    assert tenants.decrypt_secrets(ctx.crypto, t)[0] == 'Rigtig-Kode-123'
    assert set(tenants.sections(t)) == {'pln', 'msg', 'doc'}


def test_ai_requires_consent_and_valid_key(client, ctx):
    tid = signup(client, ctx)
    assert save_settings(client, ai_mode='shared').status_code == 400
    assert save_settings(client, ai_mode='own', ai_consent='on',
                         ai_key='ikke-en-noegle').status_code == 400
    assert save_settings(client, ai_mode='own', ai_consent='on',
                         ai_key='sk-ant-' + 'x' * 30 + 'nej').status_code \
        == 400
    r = save_settings(client, ai_mode='own', ai_consent='on',
                      ai_key='sk-ant-' + 'x' * 30 + 'ok')
    assert r.status_code == 303
    t = tenants.get(ctx.db(), tid)
    assert t['ai_mode'] == 'own' and t['ai_key_enc'].startswith('v1.')
    assert b'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxok' not in _db_bytes(ctx)


# Modtagere

def test_new_recipient_needs_owner_approval_and_confirmation(client, ctx):
    tid = signup(client, ctx, 'mor@example.net')
    n = len(ctx.mailer.sent)
    post(client, '/recipients/add', {'email': 'far@example.net'})
    to, _s, _b = ctx.mailer.sent[n]
    assert to == 'mor@example.net'  # godkendelse til login-mailen
    assert tenants.confirmed_recipients(ctx.db(), tid) == ['mor@example.net']

    other = new_client(client)
    approve = ctx.mailer.last_link('mor@example.net')
    assert post(other, '/auth/' + approve).status_code == 200
    assert ctx.mailer.sent[-1][0] == 'far@example.net'
    assert tenants.confirmed_recipients(ctx.db(), tid) == ['mor@example.net']

    confirm = ctx.mailer.last_link('far@example.net')
    assert post(other, '/auth/' + confirm).status_code == 200
    assert tenants.confirmed_recipients(ctx.db(), tid) == [
        'mor@example.net', 'far@example.net']


def test_cannot_touch_another_tenants_recipient(client, ctx):
    other = new_client(client)
    tid_b = signup(other, ctx, 'b@example.net')
    post(other, '/recipients/add', {'email': 'b2@example.net'})
    rid_b = [r['id'] for r in tenants.recipients(ctx.db(), tid_b)
             if r['email'] == 'b2@example.net'][0]

    signup(client, ctx, 'a@example.net')
    r = post(client, '/recipients/remove', {'id': str(rid_b)})
    assert r.status_code == 404
    assert len(tenants.recipients(ctx.db(), tid_b)) == 2


def test_login_email_cannot_be_removed(client, ctx):
    tid = signup(client, ctx, 'mor@example.net')
    rid = tenants.recipients(ctx.db(), tid)[0]['id']
    assert post(client, '/recipients/remove',
                {'id': str(rid)}).status_code == 404


# Data

def test_export_contains_no_secrets(client, ctx):
    signup(client, ctx)
    save_settings(client, ai_mode='own', ai_consent='on',
                  ai_key='sk-ant-' + 'x' * 30 + 'ok')
    r = client.get('/export')
    text = r.data.decode('utf-8')
    assert r.status_code == 200 and 'attachment' in \
        r.headers['Content-Disposition']
    assert 'Rigtig-Kode-123' not in text and 'v1.' not in text
    assert 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxok' not in text
    assert json.loads(text)['konto']['kodeord_gemt'] is True


def test_delete_removes_everything(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    path = os.path.join(ctx.settings.tenants_dir, tid)
    assert os.path.isdir(path)
    assert post(client, '/delete', {'confirm': 'nej'}).status_code == 400
    r = post(client, '/delete', {'confirm': 'SLET'})
    assert r.status_code == 303
    conn = ctx.db()
    assert tenants.get(conn, tid) is None
    for table in ('recipients', 'sessions', 'runs', 'audit', 'tokens'):
        assert conn.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0] \
            == 0, table
    assert not os.path.exists(path)
    assert client.get('/status').status_code == 302


def test_delete_accepts_confirm_typed_on_a_phone(client, ctx):
    # iOS skriver gerne "Slet " med stort begyndelsesbogstav og mellemrum
    tid = signup(client, ctx)
    assert post(client, '/delete', {'confirm': 'Slet '}).status_code == 303
    assert tenants.get(ctx.db(), tid) is None


def test_static_assets_are_versioned(client):
    page = client.get('/').data.decode('utf-8')
    for name in ('style.css', 'icon.svg', 'apple-touch-icon.png'):
        assert '/static/%s?v=' % name in page
    r = client.get('/apple-touch-icon.png')
    assert r.status_code == 200 and r.mimetype == 'image/png'


def test_logout_all_invalidates_other_sessions(client, ctx):
    signup(client, ctx, 'mor@example.net')
    other = new_client(client)
    post(other, '/login', {'email': 'mor@example.net'})
    post(other, '/auth/' + ctx.mailer.last_link())
    assert other.get('/status').status_code == 200
    post(client, '/logout-all')
    assert other.get('/status').status_code == 302
    assert client.get('/status').status_code == 200


def test_session_expires_when_idle(client, ctx):
    signup(client, ctx)
    ctx.db().execute('UPDATE sessions SET last_seen = ?',
                     (auth.now() - auth.SESSION_IDLE - 1,))
    assert client.get('/status').status_code == 302


def test_schedule_text_follows_settings(ctx):
    from app.web import run_schedule
    assert run_schedule(ctx.settings) == 'hver time kl. 6-22'
    ctx.settings = dataclasses.replace(ctx.settings, run_interval_minutes=30)
    assert run_schedule(ctx.settings) == 'hver halve time kl. 6-22'


def test_privacy_page_shows_schedule(client):
    assert 'hver time kl. 6-22'.encode() in client.get('/privatliv').data


def test_privacy_page_never_shows_admin_email(client, ctx):
    body = client.get('/privatliv').data
    assert ctx.settings.admin_email.encode() not in body
    assert b'mailto:' not in body
    assert b'https://github.com/vondk/fskintra-hosted/issues' in body


def test_redirects_to_canonical_host(ctx):
    from conftest import make_client
    c = make_client(client_app(ctx), host='fskintra-hosted.fly.dev')
    r = c.get('/status?x=1')
    assert r.status_code == 301
    assert r.headers['Location'] == PUBLIC + '/status?x=1'
    assert c.get('/healthz').status_code == 200


def client_app(ctx):
    from app.web import create_public_app
    return create_public_app(ctx)


@pytest.mark.parametrize('email', ['a b@example.net', 'æ@example.net',
                                   'a@b', 'a@example.net\r\nBcc: x@y.dk',
                                   'x' * 250 + '@example.net'])
def test_invalid_emails_are_rejected(email):
    assert tenants.normalize_email(email) is None


def test_check_login_has_short_timeout(ctx):
    from app import runner
    assert runner.CHECK_TIMEOUT <= 90 < ctx.settings.run_timeout_seconds


# Web app og login med engangskode

def test_refresh_button_gives_a_receipt(client, ctx):
    '''En web app har ingen genindlæs-knap. Et link til siden selv
    genindlæser i Safari uden synlig forskel, så Opdatér er en POST, der
    kvitterer med klokkeslættet'''
    signup(client, ctx)
    save_settings(client)
    for page, what in (('overblik', 'Overblikket'), ('status', 'Siden')):
        html = client.get('/' + page).data.decode('utf-8')
        assert '<form method="post" action="/opdater"' in html
        assert 'name="side" value="%s"' % page in html
        assert 'er opdateret kl.' not in html
        r = post(client, '/opdater', {'side': page})
        assert r.status_code == 303
        assert r.headers['Location'] == '/%s?m=updated' % page
        html = client.get(r.headers['Location']).data.decode('utf-8')
        assert re.search(r'role="status">%s er opdateret kl\. '
                         r'\d\d:\d\d:\d\d\.' % what, html)
    # Kun de to sider - aldrig en adresse fra formularen
    assert post(client, '/opdater',
                {'side': 'https://ond.example'}).status_code == 400
    assert post(client, '/opdater').status_code == 400
    assert client.get('/opdater').status_code == 405


def test_web_app_manifest(client):
    page = client.get('/').data.decode('utf-8')
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert "manifest-src 'self'" in client.get('/').headers[
        'Content-Security-Policy']
    r = client.get('/manifest.webmanifest')
    assert r.status_code == 200
    assert r.mimetype == 'application/manifest+json'
    m = json.loads(r.data)
    assert m['display'] == 'standalone' and m['start_url'] == '/'
    sizes = {(i['sizes'], i['purpose']) for i in m['icons']}
    assert {('192x192', 'any'), ('512x512', 'any'),
            ('512x512', 'maskable')} <= sizes
    for icon in m['icons']:
        assert client.get(icon['src']).mimetype == 'image/png'


def _request_code(client, email):
    r = post(client, '/login', {'email': email})
    assert r.status_code == 200 and b'name="code"' in r.data
    assert client.get_cookie('__Host-code', domain='hosted.example')


def test_login_with_code_in_the_requesting_app(client, ctx):
    signup(client, ctx, 'mor@example.net')
    app = new_client(client)  # appen på hjemmeskærmen har egne cookies
    _request_code(app, 'mor@example.net')
    code = ctx.mailer.last_code('mor@example.net')
    assert code[:3] + ' ' + code[3:] in ctx.mailer.html[-1]
    r = post(app, '/kode', {'code': code[:3] + ' ' + code[3:]})
    assert r.status_code == 303  # mellemrum som i mailen er ok
    assert app.get('/status').status_code == 200
    assert app.get_cookie('__Host-code', domain='hosted.example') is None
    # Engangs: en anden browser med en ny kode kan ikke bruge den gamle
    other = new_client(client)
    _request_code(other, 'mor@example.net')
    assert post(other, '/kode', {'code': code}).status_code == 400


def _browser_with_code_cookie(client, nonce):
    b = new_client(client)
    b.set_cookie('__Host-code', nonce, domain='hosted.example')
    return b


def test_double_tapped_code_still_logs_in(client, ctx, monkeypatch):
    '''Uden JavaScript sender et dobbelttryk formularen to gange, og
    browseren viser kun det andet svar. Det må ikke blive "forkert kode"'''
    signup(client, ctx, 'mor@example.net')
    app = new_client(client)
    _request_code(app, 'mor@example.net')
    nonce = app.get_cookie('__Host-code', domain='hosted.example').value
    code = ctx.mailer.last_code('mor@example.net')
    first = post(app, '/kode', {'code': code})
    assert first.status_code == 303
    assert first.headers['Location'] == '/settings'
    # Første svar nåede frem: andet tryk sender samme sted hen
    r = post(app, '/kode', {'code': code})
    assert r.status_code == 303 and r.headers['Location'] == '/settings'
    save_settings(app)
    r = post(app, '/kode', {'code': code})
    assert r.status_code == 303 and r.headers['Location'] == '/overblik'
    # Første svar blev droppet: samme browser (samme cookie) sender igen
    again = _browser_with_code_cookie(client, nonce)
    r = post(again, '/kode', {'code': code})
    assert r.status_code == 303
    assert again.get('/status').status_code == 200
    # Kun den rigtige kode, kun med cookien, og kun lige efter
    assert post(_browser_with_code_cookie(client, nonce), '/kode',
                {'code': '%06d' % ((int(code) + 1) % 10 ** 6)}
                ).status_code == 400
    assert post(new_client(client), '/kode',
                {'code': code}).status_code == 400
    t = auth.now() + auth.CODE_REPEAT_WINDOW + 1
    monkeypatch.setattr(auth, 'now', lambda: t)
    assert post(_browser_with_code_cookie(client, nonce), '/kode',
                {'code': code}).status_code == 400


def test_repeated_code_counts_as_attempts(client, ctx):
    signup(client, ctx, 'mor@example.net')
    app = new_client(client)
    _request_code(app, 'mor@example.net')
    nonce = app.get_cookie('__Host-code', domain='hosted.example').value
    code = ctx.mailer.last_code('mor@example.net')
    assert post(app, '/kode', {'code': code}).status_code == 303
    wrong = '%06d' % ((int(code) + 1) % 10 ** 6)
    for _ in range(auth.CODE_ATTEMPTS - 1):
        assert post(_browser_with_code_cookie(client, nonce), '/kode',
                    {'code': wrong}).status_code == 400
    assert post(_browser_with_code_cookie(client, nonce), '/kode',
                {'code': code}).status_code == 400


def test_code_only_works_in_the_browser_that_asked(client, ctx):
    signup(client, ctx, 'mor@example.net')
    asker = new_client(client)
    _request_code(asker, 'mor@example.net')
    code = ctx.mailer.last_code('mor@example.net')
    thief = new_client(client)
    assert post(thief, '/kode', {'code': code}).status_code == 400
    _request_code(thief, 'ukendt@example.net')  # egen, falsk cookie
    assert post(thief, '/kode', {'code': code}).status_code == 400
    assert thief.get('/status').status_code == 302
    assert post(asker, '/kode', {'code': code}).status_code == 303


def test_code_attempts_are_limited(client, ctx):
    signup(client, ctx, 'mor@example.net')
    app = new_client(client)
    _request_code(app, 'mor@example.net')
    code = ctx.mailer.last_code('mor@example.net')
    wrong = '%06d' % ((int(code) + 1) % 10 ** 6)
    for _ in range(auth.CODE_ATTEMPTS):
        assert post(app, '/kode', {'code': wrong}).status_code == 400
    assert post(app, '/kode', {'code': code}).status_code == 400
    assert app.get('/status').status_code == 302


def test_code_attempts_are_limited_per_address_per_day(client, ctx):
    signup(client, ctx, 'mor@example.net')
    for _ in range(2):
        app = new_client(client)
        _request_code(app, 'mor@example.net')
        for _ in range(auth.CODE_ATTEMPTS):
            post(app, '/kode', {'code': '000000'})
    ctx.db().execute('DELETE FROM rate_limits WHERE key NOT IN (SELECT key '
                     'FROM rate_limits GROUP BY key HAVING COUNT(*) = 10)')
    app = new_client(client)
    _request_code(app, 'mor@example.net')
    code = ctx.mailer.last_code('mor@example.net')
    assert post(app, '/kode', {'code': code}).status_code == 400


def test_signup_with_code_creates_account(client, ctx):
    r = post(client, '/signup', {'email': 'ny@example.net', 'invite': INVITE})
    assert r.status_code == 200 and b'name="code"' in r.data
    r = post(client, '/kode', {'code': ctx.mailer.last_code('ny@example.net')})
    assert r.status_code == 303 and r.headers['Location'].endswith(
        '/settings')
    assert tenants.get_by_email(ctx.db(), 'ny@example.net') is not None


def test_code_page_reveals_nothing_about_unknown_addresses(client, ctx):
    signup(new_client(client), ctx, 'kendt@example.net')
    a, b = new_client(client), new_client(client)
    ra = post(a, '/login', {'email': 'ukendt@example.net'})
    rb = post(b, '/login', {'email': 'kendt@example.net'})
    assert _without_csrf(ra.data) == _without_csrf(rb.data)
    assert a.get_cookie('__Host-code', domain='hosted.example')
    assert b.get_cookie('__Host-code', domain='hosted.example')
    wrong = '%06d' % ((int(ctx.mailer.last_code()) + 1) % 10 ** 6)
    fa, fb = post(a, '/kode', {'code': wrong}), post(b, '/kode',
                                                     {'code': wrong})
    assert fa.status_code == fb.status_code == 400
    assert _without_csrf(fa.data) == _without_csrf(fb.data)


def _without_csrf(html):
    return re.sub(rb'name="csrf" value="[^"]+"', b'', html)


def test_reopened_web_app_finds_the_code_form(client, ctx):
    assert client.get('/login').headers['Location'].endswith('/')
    post(client, '/login', {'email': 'a@example.net'})
    assert client.get('/login').headers['Location'].endswith('/kode')
    assert b'name="code"' in client.get('/kode').data
    assert b'href="/kode"' in client.get('/').data


# Farvetema

def _theme(client):
    c = client.get_cookie('__Host-tema', domain='hosted.example')
    return c.value if c else None


def test_theme_follows_system_by_default(client):
    page = client.get('/').data.decode('utf-8')
    assert '<html lang="da">' in page
    assert '<meta name="color-scheme" content="light dark">' in page
    assert 'media="(prefers-color-scheme: dark)"' in page
    assert 'value="system" aria-pressed="true"' in page


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_chosen_theme_is_remembered(client, theme):
    r = post(client, '/tema', {'theme': theme, 'next': '/privatliv'})
    assert r.status_code == 303 and r.headers['Location'] == '/privatliv'
    cookie = [c for c in r.headers.getlist('Set-Cookie')
              if c.startswith('__Host-tema=')][0]
    assert 'Secure' in cookie and 'HttpOnly' in cookie
    page = client.get('/').data.decode('utf-8')
    assert '<html lang="da" data-theme="%s">' % theme in page
    assert '<meta name="color-scheme" content="%s">' % theme in page
    assert 'value="%s" aria-pressed="true"' % theme in page
    assert 'value="system" aria-pressed="false"' in page
    post(client, '/tema', {'theme': 'system', 'next': '/'})
    assert _theme(client) is None
    assert '<html lang="da">' in client.get('/').data.decode('utf-8')


def test_unknown_theme_means_system(client):
    post(client, '/tema', {'theme': 'dark', 'next': '/'})
    post(client, '/tema', {'theme': '"><script>', 'next': '/'})
    assert _theme(client) is None
    client.set_cookie('__Host-tema', 'blå', domain='hosted.example')
    assert b'data-theme' not in client.get('/').data


@pytest.mark.parametrize('nxt', ['//ond.example', 'https://ond.example',
                                 '/\\ond.example', '/status\n', 'status',
                                 '/status?x=1', '', '/auth/x', '/a//b'])
def test_theme_redirect_stays_on_site(client, nxt):
    r = post(client, '/tema', {'theme': 'dark', 'next': nxt})
    assert r.status_code == 303 and r.headers['Location'] == '/'


def test_theme_switch_returns_to_a_get_page_after_post(client, ctx):
    page = post(client, '/login', {'email': 'a@example.net'}).data
    assert b'name="next" value="/kode"' in page
    assert b'name="next" value="/privatliv"' in client.get('/privatliv').data
    # Sider uden for listen (fx /auth/<token>) sender tilbage til forsiden
    assert b'name="next" value="/"' in client.get('/findes-ikke').data

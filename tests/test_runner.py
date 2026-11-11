import json
import os
import stat
import time

import pytest

from app import jobs, runner, tenants
from conftest import HOST, with_settings


def _tenant(ctx, ai_mode='off', ai_key=None, username='petjen'):
    conn = ctx.db()
    tid = tenants.create(conn, 'mor@example.net')
    tenants.update(conn, tid, hostname=HOST, username=username,
                   password_enc=ctx.crypto.encrypt('Kode-123', tid,
                                                   'password'),
                   ai_mode=ai_mode, status='active',
                   ai_key_enc=ctx.crypto.encrypt(ai_key, tid, 'ai_key')
                   if ai_key else None,
                   ai_model='claude-opus-5-5' if ai_key else None)
    return tid, tenants.get(conn, tid)


def _env_of(ctx, tid):
    path = os.path.join(ctx.settings.tenants_dir, tid, 'env.json')
    with open(path) as f:
        return json.load(f)


def test_env_is_an_allowlist(ctx):
    tid, t = _tenant(ctx)
    env = runner.build_env(ctx.settings, tid, jobs.profile_for(ctx, t))
    forbidden = ('MASTER_KEY', 'INVITE_CODE', 'AWS_SECRET_ACCESS_KEY',
                 'FSKINTRA_HA_URL', 'FSKINTRA_HA_TOKEN', 'FSKINTRA_MQTT_HOST',
                 'DATA_DIR')
    assert not set(forbidden) & set(env)
    assert env['FSKINTRA_ROOT'] == os.path.join(ctx.settings.tenants_dir,
                                                tid)
    assert env['FSKINTRA_PASSWORD'] == 'Kode-123'
    assert env['FSKINTRA_RECIPIENT_EMAIL'] == 'mor@example.net'
    assert env['FSKINTRA_STORE_MESSAGES'] == 'marker'
    assert env['FSKINTRA_HA_TODO'] == env['FSKINTRA_HA_CALENDAR'] == 'off'
    # Relativ sti: fskintra skriver overblikket i forælderens eget katalog
    assert env['FSKINTRA_OVERVIEW_JSON'] == 'overblik.json'
    assert 'FSKINTRA_ANTHROPIC_API_KEY' not in env  # ai_mode off


def test_real_subprocess_gets_only_the_allowlist(ctx):
    tid, t = _tenant(ctx)
    res = runner.run(ctx.settings, tid, jobs.profile_for(ctx, t), 'run')
    assert res.exit_code == 0
    seen = _env_of(ctx, tid)['env']
    assert 'MASTER_KEY' not in seen and 'AWS_SECRET_ACCESS_KEY' not in seen
    assert not [k for k in seen if k.startswith(('FSKINTRA_HA_URL',
                                                 'FSKINTRA_MQTT'))]


@pytest.mark.parametrize('mode,own,expect_key,expect_model', [
    ('own', 'sk-ant-egen-noegle-ok', 'sk-ant-egen-noegle-ok',
     'claude-opus-5-5'),
    ('shared', None, 'shared', 'claude-haiku-5-5'),
    ('off', None, '', ''),
])
def test_ai_key_priority(ctx, env, mode, own, expect_key, expect_model):
    tid, t = _tenant(ctx, ai_mode=mode, ai_key=own)
    p = jobs.profile_for(ctx, t)
    if expect_key == 'shared':
        expect_key = env['FSKINTRA_ANTHROPIC_API_KEY']
    assert (p.ai_key, p.ai_model) == (expect_key, expect_model)


def test_shared_ai_without_server_key_is_off(ctx):
    with_settings(ctx, shared_ai_key='')
    tid, t = _tenant(ctx, ai_mode='shared')
    assert jobs.profile_for(ctx, t).ai_key == ''


def test_tenant_dir_rejects_path_traversal(ctx):
    for bad in ('../x', 'a' * 31, '', 'A' * 32):
        with pytest.raises(ValueError):
            runner.tenant_dir(ctx.settings, bad)


def test_tenant_dir_is_private(ctx):
    tid, t = _tenant(ctx)
    path = runner.tenant_dir(ctx.settings, tid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_check_login_returns_children(ctx):
    tid, t = _tenant(ctx)
    res = runner.run(ctx.settings, tid, jobs.profile_for(ctx, t), 'check')
    assert res.exit_code == 0 and res.children() == ['Anna 3B']
    assert _env_of(ctx, tid)['args'] == ['--check-login']


def test_sections_are_passed_on(ctx):
    tid, t = _tenant(ctx)
    tenants.update(ctx.db(), tid, sections='pln,msg')
    t = tenants.get(ctx.db(), tid)
    runner.run(ctx.settings, tid, jobs.profile_for(ctx, t), 'catchup')
    assert _env_of(ctx, tid)['args'] == ['--catch-up', '--section',
                                         'pln,msg']


def test_password_is_scrubbed_from_log(ctx):
    tid, t = _tenant(ctx, username='laekker')
    profile = jobs.profile_for(ctx, t, password='LAEKKET-MARKOER-4711')
    res = runner.run(ctx.settings, tid, profile, 'run')
    assert res.exit_code == 4
    assert 'LAEKKET-MARKOER-4711' not in res.log_tail
    assert 'fejl: [skjult]' in res.log_tail


def test_timeout_kills_the_process(ctx):
    with_settings(ctx, run_timeout_seconds=1)
    tid, t = _tenant(ctx, username='sover')
    start = time.time()
    res = runner.run(ctx.settings, tid, jobs.profile_for(ctx, t), 'run')
    assert res.exit_code == runner.EXIT_TIMEOUT
    assert time.time() - start < 10


def test_busy_lock_returns_none(ctx):
    tid, t = _tenant(ctx)
    lock = runner.tenant_lock(tid)
    with lock:
        assert runner.run(ctx.settings, tid, jobs.profile_for(ctx, t),
                          'check', lock_timeout=0.1) is None


def test_cache_retention_is_handed_to_fskintra(ctx):
    '''fskintra rydder selv op i cachen - driftslaget giver kun grænsen'''
    tid, t = _tenant(ctx)
    with_settings(ctx, cache_retention_days=17)
    env = runner.build_env(ctx.settings, tid, jobs.profile_for(ctx, t))
    assert env['FSKINTRA_PRUNE_CACHE_DAYS'] == '17'
    # Markeringer af sendte mails ryddes ikke (kræver en aktiv beslutning)
    assert 'FSKINTRA_PRUNE_MARKERS_DAYS' not in env


def test_runner_does_not_know_fskintras_directories():
    with open(runner.__file__, encoding='utf-8') as f:
        source = f.read()
    for name in ("'cache'", "'msg'", "'state'", 'state.json', '.state'):
        assert name not in source

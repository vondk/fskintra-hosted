import datetime
import re

from app import auth, jobs, runner, tenants
from app.scheduler import Scheduler
from conftest import HOST, csrf, post, save_settings, signup

ADMIN_DOMAIN = 'localhost'


def apost(client, url, data=None):
    data = dict(data or {})
    data.setdefault('csrf', csrf(client, 'acsrf', ADMIN_DOMAIN))
    return client.post(url, data=data)


def _admin_login(admin_client, ctx):
    apost(admin_client, '/admin/login', {'email': 'admin@example.net'})
    body = ctx.mailer.sent[-1][2]
    token = re.search(r'/admin/auth/([A-Za-z0-9_-]+)', body).group(1)
    assert body.startswith('Log ind') and 'http://localhost:9090' in body
    return apost(admin_client, '/admin/auth/' + token)


def test_admin_requires_login(admin_client):
    r = admin_client.get('/admin')
    assert r.status_code == 302 and r.headers['Location'].endswith(
        '/admin/login')


def test_admin_login_only_for_admin_email(admin_client, ctx):
    r = apost(admin_client, '/admin/login', {'email': 'ond@example.net'})
    assert r.status_code == 200 and not ctx.mailer.sent
    r = _admin_login(admin_client, ctx)
    assert r.status_code == 303
    assert admin_client.get('/admin').status_code == 200


def test_public_session_is_not_an_admin_session(client, admin_client, ctx):
    signup(client, ctx, 'admin@example.net')
    sid = client.get_cookie('__Host-sid', domain='hosted.example').value
    admin_client.set_cookie('asid', sid, domain=ADMIN_DOMAIN)
    assert admin_client.get('/admin').status_code == 302


def test_admin_never_shows_secrets(client, admin_client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    _admin_login(admin_client, ctx)
    page = admin_client.get('/admin/t/' + tid).data
    assert b'Rigtig-Kode-123' not in page and b'v1.' not in page
    assert HOST.encode() in page


# Planlægger og resultater

def _active(client, ctx, email='mor@example.net', username='petjen'):
    tid = signup(client, ctx, email)
    assert save_settings(client, username=username).status_code == 303
    return tid


def test_first_run_is_catchup_then_normal(client, ctx):
    tid = _active(client, ctx)
    s = Scheduler(ctx, jitter=(0, 0))
    s.tick(datetime.datetime(2026, 11, 4, 3, 0))  # uden for tidsrum
    t = tenants.get(ctx.db(), tid)
    assert t['needs_catchup'] == 0 and t['last_exit'] == 0
    kinds = [r['kind'] for r in tenants.runs(ctx.db(), tid)]
    assert kinds == ['catchup', 'check']


def test_cycle_runs_active_tenants_within_hours(client, ctx):
    tid = _active(client, ctx)
    tenants.update(ctx.db(), tid, needs_catchup=0)
    s = Scheduler(ctx, jitter=(0, 0))
    s.tick(datetime.datetime(2026, 11, 4, 3, 0))
    assert len(tenants.runs(ctx.db(), tid)) == 1  # kun check fra settings
    s.tick(datetime.datetime(2026, 11, 4, 10, 0))
    assert tenants.runs(ctx.db(), tid)[0]['kind'] == 'run'
    s.tick(datetime.datetime(2026, 11, 4, 10, 5))  # samme slot
    assert len(tenants.runs(ctx.db(), tid)) == 2


def test_paused_tenants_are_skipped(client, ctx):
    tid = _active(client, ctx)
    post(client, '/pause')
    Scheduler(ctx, jitter=(0, 0)).tick(datetime.datetime(2026, 11, 4, 10, 0))
    assert [r['kind'] for r in tenants.runs(ctx.db(), tid)] == ['check']


def test_login_failure_pauses_and_mails(client, ctx):
    tid = _active(client, ctx)
    tenants.update(ctx.db(), tid, username='forkert', needs_catchup=0)
    jobs.execute(ctx, tid, 'run')
    t = tenants.get(ctx.db(), tid)
    assert (t['status'], t['pause_reason']) == ('paused', 'login')
    to, subject, body = ctx.mailer.sent[-1]
    assert to == 'mor@example.net' and 'afviste' in subject
    # Ingen nye forsøg mens den er på pause
    assert jobs.execute(ctx, tid, 'run') is None


def test_repeated_errors_alert_admin_once(client, ctx):
    tid = _active(client, ctx)
    tenants.update(ctx.db(), tid, username='nede', needs_catchup=0)
    for _ in range(jobs.ADMIN_ALERT_AFTER + 2):
        jobs.execute(ctx, tid, 'run')
    alerts = [m for m in ctx.mailer.sent if m[0] == 'admin@example.net']
    assert len(alerts) == 1
    assert tenants.get(ctx.db(), tid)['status'] == 'active'


def test_new_login_after_pause_reactivates(client, ctx):
    tid = _active(client, ctx)
    tenants.update(ctx.db(), tid, status='paused', pause_reason='login')
    assert save_settings(client, password='Ny-Kode-456').status_code == 303
    assert tenants.get(ctx.db(), tid)['status'] == 'active'


def test_housekeeping_removes_expired(ctx):
    conn = ctx.db()
    auth.new_token(conn, 'login', 'x@example.net')
    conn.execute('UPDATE tokens SET expires_at = 1')
    auth.housekeeping(conn)
    assert conn.execute('SELECT COUNT(*) FROM tokens').fetchone()[0] == 0


def test_scheduler_lock_is_exclusive(ctx):
    a, b = Scheduler(ctx), Scheduler(ctx)
    assert a.acquire_lock()
    assert not b.acquire_lock()


def test_runner_exit_constants_match_fskintra():
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'vendor',
                        'fskintra', 'skoleintra', 'config.py')
    src = open(path, encoding='utf-8').read()
    assert 'EXIT_LOGIN_FAILED = %d' % runner.EXIT_LOGIN_FAILED in src
    assert 'EXIT_SITE_ERROR = %d' % runner.EXIT_SITE_ERROR in src
    assert importlib.util  # noqa

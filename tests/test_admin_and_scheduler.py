import datetime
import re
import sqlite3

from app import auth, jobs, runner, tenants
from app.scheduler import Scheduler
from conftest import (HOST, csrf, new_client, post, save_settings,
                      signup)

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


def _kinds(ctx, tid):
    return [r['kind'] for r in reversed(tenants.runs(ctx.db(), tid))]


def test_exception_in_one_tenant_does_not_stop_others(client, ctx,
                                                      monkeypatch):
    # a har aldrig kørt og sorteres først; b kører efter a
    a = _active(client, ctx, 'a@example.net')
    b = _active(new_client(client), ctx, 'b@example.net')
    tenants.update(ctx.db(), a, needs_catchup=0)
    tenants.update(ctx.db(), b, needs_catchup=0, last_run_at=1)
    real_record_run = tenants.record_run

    def record_run(conn, tid, result):
        if tid == a:
            raise sqlite3.OperationalError('database or disk is full')
        return real_record_run(conn, tid, result)

    monkeypatch.setattr(tenants, 'record_run', record_run)
    s = Scheduler(ctx, jitter=(0, 0))
    s.tick(datetime.datetime(2026, 11, 4, 10, 0))
    assert _kinds(ctx, b) == ['check', 'run']
    t = tenants.get(ctx.db(), a)
    # a låser ikke rækkefølgen fast: den er talt som fejl og har fået
    # last_run_at, så den ikke længere sorteres først som "aldrig kørt"
    assert t['fail_count'] == 1 and t['last_run_at'] is not None
    s.tick(datetime.datetime(2026, 11, 4, 11, 0))
    assert _kinds(ctx, b) == ['check', 'run', 'run']
    assert tenants.get(ctx.db(), a)['fail_count'] == 2


def test_exception_in_pending_does_not_stop_fixed_run(client, ctx,
                                                      monkeypatch):
    a = _active(client, ctx, 'a@example.net')          # catch-up venter
    b = _active(new_client(client), ctx, 'b@example.net')
    tenants.update(ctx.db(), b, needs_catchup=0)
    real_execute = jobs.execute
    calls = []

    def execute(c, tid, kind, *args, **kw):
        calls.append((tid, kind))
        if tid == a:
            raise OSError('Popen fejlede')
        return real_execute(c, tid, kind, *args, **kw)

    monkeypatch.setattr(jobs, 'execute', execute)
    s = Scheduler(ctx, jitter=(0, 0))
    s.tick(datetime.datetime(2026, 11, 4, 10, 0))
    # a prøves én gang i trin 1 og ikke igen i samme tick; b kører i trin 2
    assert calls == [(a, 'catchup'), (b, 'run')]
    t = tenants.get(ctx.db(), a)
    assert t['fail_count'] == 1 and t['last_run_at']
    assert t['needs_catchup'] == 1


def test_failed_catchup_waits_for_next_slot(client, ctx):
    tid = _active(client, ctx)
    tenants.update(ctx.db(), tid, username='nede')   # exit 4
    s = Scheduler(ctx, jitter=(0, 0))
    s.tick(datetime.datetime(2026, 11, 4, 10, 0))
    assert _kinds(ctx, tid) == ['check', 'catchup']
    t = tenants.get(ctx.db(), tid)
    assert (t['fail_count'], t['last_exit'], t['needs_catchup']) == (1, 4, 1)

    # Næste ticks i samme slot: ingen nye forsøg mod skolens server
    for minute in (0, 1, 30, 59):
        s.tick(datetime.datetime(2026, 11, 4, 10, minute, 10))
    assert _kinds(ctx, tid) == ['check', 'catchup']

    # Næste faste slot: catch-up (ikke en almindelig kørsel) prøves igen
    s.tick(datetime.datetime(2026, 11, 4, 11, 0))
    assert _kinds(ctx, tid) == ['check', 'catchup', 'catchup']
    assert tenants.get(ctx.db(), tid)['fail_count'] == 2

    # Skolen er oppe igen: catch-up lykkes og alt nulstilles
    tenants.update(ctx.db(), tid, username='petjen')
    s.tick(datetime.datetime(2026, 11, 4, 11, 0, 10))   # stadig samme slot
    assert len(_kinds(ctx, tid)) == 3
    s.tick(datetime.datetime(2026, 11, 4, 12, 0))
    t = tenants.get(ctx.db(), tid)
    assert (t['fail_count'], t['needs_catchup'], t['last_exit']) == (0, 0, 0)
    s.tick(datetime.datetime(2026, 11, 4, 13, 0))
    assert _kinds(ctx, tid)[-2:] == ['catchup', 'run']


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


def test_admin_has_no_theme_switch(admin_client):
    # /tema findes kun i den offentlige app; admin følger systemet
    assert b'/tema' not in admin_client.get('/admin/login').data

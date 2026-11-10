import datetime
import json
import os

from app import db, retention, runner, tenants
from app.db import now

DAY = 86400


def _tenant(ctx, status='paused', idle_days=0, email='mor@example.net'):
    conn = ctx.db()
    tid = tenants.create(conn, email)
    t = now() - idle_days * DAY
    conn.execute('UPDATE tenants SET status = ?, created_at = ?, '
                 'last_run_at = ?, needs_catchup = 0 WHERE id = ?',
                 (status, t, None if status == 'new' else t, tid))
    return tid


def _files(ctx, tid):
    home = runner.tenant_dir(ctx.settings, tid)
    for d in ('cache', 'state', 'msg'):
        os.makedirs(os.path.join(home, d), exist_ok=True)
        with open(os.path.join(home, d, 'x'), 'w') as f:
            f.write('x')
    with open(os.path.join(home, 'overblik.json'), 'w') as f:
        f.write('{}')
    return home


def test_logs_older_than_30_days_are_deleted(ctx):
    tid = _tenant(ctx, 'active')
    conn = ctx.db()
    old = now() - 31 * DAY
    conn.execute('INSERT INTO runs (tenant_id, kind, started_at) '
                 "VALUES (?, 'run', ?)", (tid, old))
    conn.execute("INSERT INTO audit (tenant_id, ts, event) VALUES (?, ?, 'x')",
                 (tid, old))
    retention.sweep(ctx)
    assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit WHERE event = 'x'"
                        ).fetchone()[0] == 0


def test_unfinished_signup_is_deleted_after_a_week(ctx):
    fresh = _tenant(ctx, 'new', 6, 'ny@example.net')
    stale = _tenant(ctx, 'new', 8, 'gammel@example.net')
    home = _files(ctx, stale)
    retention.sweep(ctx)
    assert tenants.get(ctx.db(), fresh) is not None
    assert tenants.get(ctx.db(), stale) is None and not os.path.exists(home)


def test_active_tenants_are_never_touched(ctx):
    tid = _tenant(ctx, 'active', 400)
    home = _files(ctx, tid)
    retention.sweep(ctx)
    assert tenants.get(ctx.db(), tid) is not None
    assert os.path.exists(os.path.join(home, 'cache', 'x'))
    assert not ctx.mailer.sent


def test_paused_tenant_files_are_purged_after_retention(ctx):
    young = _tenant(ctx, 'paused', 29, 'ung@example.net')
    old = _tenant(ctx, 'paused', 31, 'gammel@example.net')
    hy, ho = _files(ctx, young), _files(ctx, old)
    retention.sweep(ctx)
    assert os.path.exists(os.path.join(hy, 'cache', 'x'))
    assert not any(os.path.lexists(os.path.join(ho, n))
                   for n in ('cache', 'state', 'msg', 'overblik.json'))
    t = tenants.get(ctx.db(), old)
    # Markeringerne er væk, så første kørsel efter pausen skal være catch-up
    assert t['status'] == 'paused' and t['needs_catchup'] == 1
    retention.sweep(ctx)  # idempotent
    assert tenants.get(ctx.db(), young)['needs_catchup'] == 0


def test_purge_skipped_while_a_run_holds_the_lock(ctx):
    tid = _tenant(ctx, 'paused', 40)
    home = _files(ctx, tid)
    with runner.tenant_lock(tid):
        retention.sweep(ctx)
    assert os.path.exists(os.path.join(home, 'cache', 'x'))


def test_inactive_account_is_warned_once_then_deleted(ctx):
    tid = _tenant(ctx, 'paused', 77)
    retention.sweep(ctx)
    retention.sweep(ctx)
    warnings = [m for m in ctx.mailer.sent if 'slettes' in m[1]]
    assert len(warnings) == 1 and warnings[0][0] == 'mor@example.net'
    assert tenants.get(ctx.db(), tid) is not None
    ctx.db().execute('UPDATE tenants SET last_run_at = ? WHERE id = ?',
                     (now() - 91 * DAY, tid))
    retention.sweep(ctx)
    assert tenants.get(ctx.db(), tid) is None


def test_no_warning_before_the_last_two_weeks(ctx):
    _tenant(ctx, 'paused', 60)
    retention.sweep(ctx)
    assert not ctx.mailer.sent


def test_failed_warning_mail_is_retried(ctx):
    tid = _tenant(ctx, 'paused', 80)
    ctx.mailer.send = lambda *a, **k: False
    retention.sweep(ctx)
    assert tenants.get(ctx.db(), tid)['inactive_warned_at'] is None


def _state(home, changes, version=1):
    os.makedirs(os.path.join(home, 'state'), exist_ok=True)
    path = os.path.join(home, 'state', 'state.json')
    with open(path, 'w') as f:
        json.dump({'version': version, 'weekplans': {'Anna': {'1-2026': {}}},
                   'last_change': changes}, f)
    return path


def _when(days):
    return (datetime.datetime.now() -
            datetime.timedelta(days=days)).isoformat(timespec='seconds')


def test_prune_state_removes_old_last_change_only(tmp_path):
    path = _state(str(tmp_path), {'Anna': {'when': _when(20)},
                                  'Bo': {'when': _when(2)}})
    assert runner.prune_state(str(tmp_path)) is True
    data = json.load(open(path))
    assert list(data['last_change']) == ['Bo']
    assert data['weekplans'] == {'Anna': {'1-2026': {}}}
    assert runner.prune_state(str(tmp_path)) is False


def test_prune_state_ignores_unknown_formats(tmp_path):
    path = _state(str(tmp_path), {'Anna': {'when': _when(99)}}, version=2)
    before = open(path).read()
    assert runner.prune_state(str(tmp_path)) is False
    assert open(path).read() == before
    open(path, 'w').write('ikke json')
    assert runner.prune_state(str(tmp_path)) is False
    open(path, 'w').write('[]')
    assert runner.prune_state(str(tmp_path)) is False


def test_migration_adds_warned_column(ctx):
    cols = [r[1] for r in ctx.db().execute('PRAGMA table_info(tenants)')]
    assert 'inactive_warned_at' in cols
    assert ctx.db().execute('PRAGMA user_version').fetchone()[0] == \
        len(db.MIGRATIONS)


def test_privacy_page_states_the_actual_limits(client, ctx):
    r = client.get('/privatliv')
    html = r.get_data(as_text=True)
    for needle in ('30 dage', '14 dage', '90 dage', '7 dage',
                   'Login-tilstand', 'smtp.example.net'):
        assert needle in html, needle

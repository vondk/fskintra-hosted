import json
import os
import subprocess
import sys

from app import db, overview, retention, runner, tenants
from app.db import now
from conftest import with_settings

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


REAL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'vendor', 'fskintra', 'fskintra.py')


def test_purge_uses_fskintra_without_login(ctx):
    '''Sletningen sker i fskintra (--purge), og miljøet indeholder hverken
    login eller SMTP-opsætning'''
    tid = _tenant(ctx, 'paused', 31)
    home = _files(ctx, tid)
    assert runner.purge_files(ctx.settings, home) is True
    with open(os.path.join(home, 'env.json')) as f:
        seen = json.load(f)
    assert seen['args'] == ['--purge']
    assert seen['env']['FSKINTRA_ROOT'] == home
    assert not [k for k in seen['env'] if 'PASSWORD' in k or 'SMTP' in k
                or 'USERNAME' in k or 'KEY' in k]
    assert runner.purge_files(ctx.settings, home) is False   # intet tilbage


def test_purge_of_missing_home_is_false(ctx, tmp_path):
    assert runner.purge_files(ctx.settings, str(tmp_path / 'nej')) is False
    assert not (tmp_path / 'nej').exists()


def test_purge_survives_a_broken_fskintra(ctx, tmp_path):
    home = _files(ctx, _tenant(ctx, 'paused', 31))
    broken = tmp_path / 'itu.py'
    broken.write_text('import sys; print("ikke json"); sys.exit(1)')
    with_settings(ctx, fskintra_path=str(broken))
    assert runner.purge_files(ctx.settings, home) is False
    with_settings(ctx, fskintra_path=str(tmp_path / 'findes-ikke.py'))
    assert runner.purge_files(ctx.settings, home) is False


def test_real_fskintra_purges_what_a_run_leaves_behind(ctx):
    '''Kontrakten med submodulet: det rigtige fskintra sletter cache,
    markeringer, tilstand og overblik for forælderens katalog - og kun det'''
    with_settings(ctx, fskintra_path=REAL)
    home = _files(ctx, _tenant(ctx, 'paused', 31))
    with open(os.path.join(home, 'cache', 'skole-bruger.state'), 'w') as f:
        f.write('cookies')
    assert runner.purge_files(ctx.settings, home) is True
    assert os.listdir(home) == []
    assert runner.purge_files(ctx.settings, home) is False


def test_real_fskintra_paths_match_the_overview_file(ctx):
    '''/overblik læser overblik.json i forælderens katalog - det skal være
    den fil, fskintra skriver (og sletter)'''
    home = runner.tenant_dir(ctx.settings, _tenant(ctx, 'active'))
    out = subprocess.run([sys.executable, REAL, '--print-paths'],
                         env=runner.base_env(home), capture_output=True,
                         text=True, check=True)
    paths = json.loads(out.stdout)
    assert paths['version'] == 1
    assert paths['overview_json'] == os.path.join(home, overview.FILENAME)
    assert paths['root'] == home


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

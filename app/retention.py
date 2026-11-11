"""Opbevaring: sletter data, der ikke længere skal gemmes. Kører fra
planlæggeren og dækker det, den løbende oprydning ikke når - især forældre,
hvor hentningen står på pause, og som derfor aldrig kører igen.

* Kørselslog og hændelser: højst LOG_DAYS dage (ud over rækkegrænsen)
* Tilmeldinger uden gemt login: slettes efter unfinished_signup_days
* Pause: hentede filer slettes efter cache_retention_days. Kontoen får et
  varsel WARN_DAYS før den slettes efter inactive_account_days
"""

import logging

from . import mails, runner, tenants
from .db import now

log = logging.getLogger(__name__)

LOG_DAYS = 30
WARN_DAYS = 14
DAY = 86400


def prune_logs(conn):
    limit = now() - LOG_DAYS * DAY
    conn.execute('DELETE FROM runs WHERE started_at < ?', (limit,))
    conn.execute('DELETE FROM audit WHERE ts < ?', (limit,))


def _purge(ctx, tenant):
    '''Slet hentede filer for en forælder uden aktiv hentning. Springes over,
    hvis en kørsel står på. Næste kørsel er en catch-up, så de slettede
    markeringer ikke giver gensendte mails'''
    tid = tenant['id']
    lock = runner.tenant_lock(tid)
    if not lock.acquire(blocking=False):
        return
    try:
        home = '%s/%s' % (ctx.settings.tenants_dir, tid)
        if runner.purge_files(ctx.settings, home):
            conn = ctx.db()
            tenants.update(conn, tid, needs_catchup=1)
            tenants.audit(conn, tid, 'Hentede data slettet efter længere '
                                     'tids pause')
    finally:
        lock.release()


def sweep(ctx):
    '''Én gennemgang af alle forældre uden aktiv hentning'''
    s = ctx.settings
    conn = ctx.db()
    t = now()
    prune_logs(conn)
    for tenant in conn.execute(
            "SELECT * FROM tenants WHERE status != 'active'").fetchall():
        tid = tenant['id']
        since = tenant['last_run_at'] or tenant['created_at']
        idle = t - since
        if tenant['status'] == 'new':
            # Aldrig færdigmeldt: der er kun en mailadresse at slette
            if t - tenant['created_at'] > s.unfinished_signup_days * DAY:
                tenants.delete(conn, s, tid)
                log.info('Slettede ufærdig tilmelding tenant=%s', tid[:8])
            continue
        if idle > s.inactive_account_days * DAY:
            tenants.delete(conn, s, tid)
            log.info('Slettede inaktiv konto tenant=%s', tid[:8])
            continue
        if idle > s.cache_retention_days * DAY:
            _purge(ctx, tenant)
        days_left = s.inactive_account_days - idle // DAY
        if days_left <= WARN_DAYS and \
                (tenant['inactive_warned_at'] or 0) < since:
            if ctx.send_mail(tenant['email'], mails.inactive_warning(
                    s.public_url + '/settings', max(1, days_left),
                    s.inactive_account_days)):
                tenants.update(conn, tid, inactive_warned_at=t)

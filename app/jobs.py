"""Kørsler for en forælder og håndtering af resultatet (pause, mails)."""

import logging

from . import runner, tenants
from .db import now

log = logging.getLogger(__name__)

ADMIN_ALERT_AFTER = 3   # fejl i træk før admin får besked


def profile_for(ctx, tenant, password=None, overrides=None):
    '''Byg en runner.Profile. password/overrides bruges når nye
    loginoplysninger skal testes før de gemmes'''
    conn = ctx.db()
    saved_password, own_key = tenants.decrypt_secrets(ctx.crypto, tenant)
    o = dict(overrides or {})

    ai_key, ai_model = '', ''
    if tenant['ai_mode'] == 'own' and own_key:
        ai_key, ai_model = own_key, tenant['ai_model'] or ''
    elif tenant['ai_mode'] == 'shared' and ctx.settings.shared_ai_key:
        ai_key = ctx.settings.shared_ai_key
        ai_model = ctx.settings.shared_ai_model

    recipients = tenants.confirmed_recipients(conn, tenant['id'])
    return runner.Profile(
        logintype=o.get('logintype', tenant['logintype']),
        hostname=o.get('hostname', tenant['hostname'] or ''),
        username=o.get('username', tenant['username'] or ''),
        password=password if password is not None else saved_password,
        recipients=recipients or [tenant['email']],
        sections=tenants.sections(tenant),
        ai_key=ai_key,
        ai_model=ai_model,
    )


def execute(ctx, tenant_id, kind, lock_timeout=-1):
    '''Kør catchup/run for en aktiv forælder og håndtér resultatet'''
    conn = ctx.db()
    tenant = tenants.get(conn, tenant_id)
    if tenant is None or tenant['status'] != 'active':
        return None
    profile = profile_for(ctx, tenant)
    if not profile.password:
        tenants.update(conn, tenant_id, status='paused',
                       pause_reason='login')
        return None
    result = runner.run(ctx.settings, tenant_id, profile, kind, lock_timeout)
    if result is None:
        return None
    # Forælderen kan være slettet mens kørslen stod på
    if tenants.get(conn, tenant_id) is None:
        return result
    tenants.record_run(conn, tenant_id, result)
    handle_result(ctx, tenant, result)
    log.info('kørsel tenant=%s type=%s exit=%s varighed=%.1fs',
             tenant_id[:8], kind, result.exit_code, result.duration)
    return result


def handle_result(ctx, tenant, result):
    conn = ctx.db()
    tid = tenant['id']
    fields = {'last_run_at': now(), 'last_exit': result.exit_code}

    if result.exit_code == runner.EXIT_OK:
        fields['fail_count'] = 0
        if result.kind == 'catchup':
            fields['needs_catchup'] = 0
            tenants.audit(conn, tid, 'Første kørsel gennemført - nye beskeder '
                                     'sendes fremover')
    elif result.exit_code == runner.EXIT_LOGIN_FAILED:
        # Stop med det samme - gentagne forkerte logins kan låse kontoen
        fields.update(status='paused', pause_reason='login')
        tenants.audit(conn, tid, 'Sat på pause: ForældreIntra afviste login')
        ctx.mailer.send(
            tenant['email'], 'fskintra: ForældreIntra afviste dit login',
            'ForældreIntra afviste brugernavn eller kodeord ved seneste '
            'kørsel, så vi har sat hentningen på pause for ikke at låse din '
            'konto.\n\nOpdatér kodeordet her:\n%s/settings\n'
            % ctx.settings.public_url)
    else:
        fields['fail_count'] = (tenant['fail_count'] or 0) + 1
        if fields['fail_count'] == ADMIN_ALERT_AFTER:
            ctx.mailer.send(
                ctx.settings.admin_email,
                'fskintra: %d fejl i træk for en forælder' % ADMIN_ALERT_AFTER,
                'Tenant %s (%s) har fejlet %d gange i træk. Seneste exit-'
                'kode: %s.\n\nSe detaljer i admin: %s/admin/t/%s\n'
                % (tid[:8], tenant['hostname'], ADMIN_ALERT_AFTER,
                   result.exit_code, ctx.settings.admin_url, tid))
    tenants.update(conn, tid, **fields)

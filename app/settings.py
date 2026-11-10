"""Indstillinger fra miljøet. Alle hemmeligheder kommer fra Fly secrets -
intet i koden eller repoet."""

import base64
import binascii
import os
import re
from dataclasses import dataclass, field

# ForældreIntra-domæner. Brugerne vælger kun blandt ALLOWED_SCHOOL_HOSTS, og
# hver af dem skal desuden matche dette mønster - serveren laver HTTP-kald
# til værten, så en fri værdi ville give SSRF mod interne adresser
SCHOOL_HOST_RE = re.compile(r'^[a-z0-9-]+\.m\.skoleintra\.dk$')

# Afsnit i fskintra (config.Section) som brugerne kan vælge imellem
SECTIONS = (
    ('frp', 'Forside og opslagstavle'),
    ('msg', 'Beskeder'),
    ('pln', 'Ugeplaner'),
    ('hwk', 'Lektier'),
    ('doc', 'Dokumenter'),
    ('pht', 'Billeder'),
    ('ctc', 'Kontaktinformation'),
    ('sgn', 'Tilmelding til samtaler/arrangementer'),
)
DEFAULT_SECTIONS = ('frp', 'msg', 'pln', 'hwk', 'doc', 'pht')

# Modeller en forælder med egen API-nøgle kan vælge
AI_MODELS = (
    ('claude-haiku-5-5', 'Claude Haiku 5.5 (billigst)'),
    ('claude-sonnet-5-5', 'Claude Sonnet 5.5'),
    ('claude-opus-5-5', 'Claude Opus 5.5 (bedst, dyrest)'),
)


class SettingsError(Exception):
    pass


def _key(name, required=True):
    raw = os.environ.get(name, '').strip()
    if not raw:
        if required:
            raise SettingsError('%s skal angives (32 bytes, base64)' % name)
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise SettingsError('%s er ikke gyldig base64' % name)
    if len(key) != 32:
        raise SettingsError('%s skal være præcis 32 bytes' % name)
    return key


def _required(name):
    value = os.environ.get(name, '').strip()
    if not value:
        raise SettingsError('%s skal angives' % name)
    return value


@dataclass(frozen=True)
class Smtp:
    sender: str
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    verify_ssl: bool


@dataclass(frozen=True)
class Settings:
    data_dir: str
    public_url: str
    admin_url: str
    admin_email: str
    operator_name: str
    invite_code: str = field(repr=False)
    master_key: bytes = field(repr=False)
    master_key_old: bytes = field(repr=False)
    allowed_school_hosts: tuple
    smtp: Smtp
    shared_ai_key: str = field(repr=False)
    shared_ai_model: str
    fskintra_path: str
    run_hours: tuple
    run_interval_minutes: int
    cache_retention_days: int
    inactive_account_days: int
    unfinished_signup_days: int
    run_timeout_seconds: int
    trust_fly_client_ip: bool
    admin_port: int
    admin_bind: str
    ai_proxy_port: int
    # Fly-releasenummer fra entrypoint.sh (fx 'v14'), tomt uden for Fly
    version: str = ''

    @property
    def db_path(self):
        return os.path.join(self.data_dir, 'app.db')

    @property
    def tenants_dir(self):
        return os.path.join(self.data_dir, 'tenants')


def load():
    '''Læs og validér indstillingerne. Fejler hellere end at starte med en
    usikker opsætning'''
    env = os.environ

    hosts = tuple(h.strip().lower()
                  for h in env.get('ALLOWED_SCHOOL_HOSTS', '').split(',')
                  if h.strip())
    if not hosts:
        raise SettingsError('ALLOWED_SCHOOL_HOSTS skal angives')
    for h in hosts:
        if not SCHOOL_HOST_RE.match(h):
            raise SettingsError('Ugyldig skole i ALLOWED_SCHOOL_HOSTS: %r' % h)

    public_url = _required('PUBLIC_URL').rstrip('/')
    if not public_url.startswith('https://') and \
            env.get('ALLOW_INSECURE_PUBLIC_URL') != '1':
        raise SettingsError('PUBLIC_URL skal starte med https://')

    invite = _required('INVITE_CODE')
    if len(invite) < 12:
        raise SettingsError('INVITE_CODE skal være mindst 12 tegn')

    smtp_user = env.get('FSKINTRA_SMTP_USER', '').strip()
    smtp_password = env.get('FSKINTRA_SMTP_PASSWORD', '')
    if bool(smtp_user) != bool(smtp_password):
        raise SettingsError('FSKINTRA_SMTP_USER og FSKINTRA_SMTP_PASSWORD '
                            'skal angives sammen')
    smtp = Smtp(
        sender=_required('FSKINTRA_SENDER_EMAIL'),
        host=env.get('FSKINTRA_SMTP_HOST', 'localhost').strip(),
        port=int(env.get('FSKINTRA_SMTP_PORT', '587')),
        user=smtp_user,
        password=smtp_password,
        verify_ssl=env.get('FSKINTRA_SMTP_VERIFY_SSL', 'true').lower()
        != 'false',
    )

    hours = env.get('RUN_HOURS', '6-22').split('-')
    run_hours = (int(hours[0]), int(hours[1]))
    if not (0 <= run_hours[0] <= run_hours[1] <= 23):
        raise SettingsError('RUN_HOURS skal være fx 6-22')

    model = env.get('SHARED_AI_MODEL', 'claude-haiku-5-5').strip()
    if model not in dict(AI_MODELS):
        raise SettingsError('SHARED_AI_MODEL er ikke en kendt model')

    return Settings(
        data_dir=env.get('DATA_DIR', '/data'),
        public_url=public_url,
        admin_url=env.get('ADMIN_URL', 'http://localhost:9090').rstrip('/'),
        admin_email=_required('ADMIN_EMAIL').lower(),
        # Vises i mails og på siden, fx "lavet af Kim von Müllen"
        operator_name=env.get('OPERATOR_NAME', '').strip(),
        invite_code=invite,
        master_key=_key('MASTER_KEY'),
        master_key_old=_key('MASTER_KEY_OLD', required=False),
        allowed_school_hosts=hosts,
        smtp=smtp,
        shared_ai_key=env.get('FSKINTRA_ANTHROPIC_API_KEY', '').strip(),
        shared_ai_model=model,
        fskintra_path=env.get(
            'FSKINTRA_PATH',
            os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), 'vendor', 'fskintra',
                'fskintra.py')),
        run_hours=run_hours,
        run_interval_minutes=max(30, int(env.get('RUN_INTERVAL_MINUTES',
                                                 '60'))),
        cache_retention_days=max(1, int(env.get('CACHE_RETENTION_DAYS',
                                                '30'))),
        # Konti uden aktiv hentning (pause) slettes efter så mange dage. Min.
        # 30, så varslet (14 dage før) og cachen (30 dage) når at virke
        inactive_account_days=max(30, int(env.get('INACTIVE_ACCOUNT_DAYS',
                                                  '90'))),
        # Tilmeldinger, hvor ForældreIntra-login aldrig blev gemt
        unfinished_signup_days=max(1, int(env.get('UNFINISHED_SIGNUP_DAYS',
                                                  '7'))),
        run_timeout_seconds=int(env.get('RUN_TIMEOUT_SECONDS', '600')),
        trust_fly_client_ip=env.get('TRUST_FLY_CLIENT_IP', '1') == '1',
        admin_port=int(env.get('ADMIN_PORT', '9090')),
        # '::' = Fly 6PN (IPv6), som `fly proxy` forbinder til
        admin_bind=env.get('ADMIN_BIND', '::'),
        # Kun på 127.0.0.1 - se aiproxy.py
        ai_proxy_port=int(env.get('AI_PROXY_PORT', '9191')),
        version=env.get('APP_VERSION', '').strip()[:32],
    )

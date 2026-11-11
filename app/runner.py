"""Kør fskintra for én forælder i en isoleret subprocess.

* Miljøet bygges fra bunden (allowlist) - hverken MASTER_KEY, andre
  forældres data eller Home Assistant-opsætning arves fra serveren.
* Eget katalog (0700) pr. forælder via FSKINTRA_ROOT, umask 077.
* Egen procesgruppe, ressourcegrænser og timeout der dræber hele gruppen.
* Kodeordet fjernes fra den log-hale der gemmes.
* Oprydning og sletning af det hentede sker i fskintra selv
  (FSKINTRA_PRUNE_CACHE_DAYS og --purge) - katalogerne kendes ikke her.
"""

import json
import logging
import os
import resource
import signal
# Kun en fast kommando uden shell, se _run
import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass

from .overview import FILENAME as OVERVIEW_FILENAME
from .tenants import TENANT_ID_RE

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_LOGIN_FAILED = 3
EXIT_SITE_ERROR = 4
EXIT_TIMEOUT = 124

LOG_TAIL = 4000
CHECK_TIMEOUT = 90   # login-tjek sker mens brugeren venter på svar
KINDS = {'check': ['--check-login'], 'catchup': ['--catch-up'], 'run': []}

_locks = {}
_locks_guard = threading.Lock()


@dataclass
class Profile:
    '''Alt en kørsel skal bruge - dekrypteret i hukommelsen'''
    logintype: str
    hostname: str
    username: str
    password: str
    recipients: list
    sections: list
    ai_key: str = ''
    ai_model: str = ''
    # Med en adresse er ai_key et engangstoken til aiproxy.py
    ai_base_url: str = ''


@dataclass
class RunResult:
    kind: str
    started_at: int
    duration: float
    exit_code: int
    stdout: str
    log_tail: str

    def children(self):
        '''Børnene fundet af --check-login'''
        try:
            data = json.loads(self.stdout.strip().splitlines()[-1])
            return [str(c) for c in data.get('children', [])]
        except (ValueError, IndexError, AttributeError):
            return []


def tenant_lock(tenant_id):
    with _locks_guard:
        return _locks.setdefault(tenant_id, threading.Lock())


def tenant_dir(settings, tenant_id):
    if not TENANT_ID_RE.match(tenant_id or ''):
        raise ValueError('ugyldigt tenant-id')
    path = os.path.join(settings.tenants_dir, tenant_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def base_env(home):
    '''Det mindste miljø: nok til, at fskintra kan finde sine data
    (--purge), men uden login'''
    env = {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': home,
        'LANG': os.environ.get('LANG', 'da_DK.UTF-8'),
        'LC_ALL': os.environ.get('LC_ALL', 'da_DK.UTF-8'),
        'TZ': os.environ.get('TZ', 'Europe/Copenhagen'),
        'PYTHONIOENCODING': 'UTF-8',
        'PYTHONUNBUFFERED': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
        'MALLOC_ARENA_MAX': '2',
        'FSKINTRA_ROOT': home,
        # Overblikket til /overblik og kalenderen (relativ til FSKINTRA_ROOT)
        'FSKINTRA_OVERVIEW_JSON': OVERVIEW_FILENAME,
    }
    if 'LOCPATH' in os.environ:  # kun til lokale tests uden systemlocale
        env['LOCPATH'] = os.environ['LOCPATH']
    return env


def build_env(settings, tenant_id, profile):
    '''Miljøet til fskintra - KUN disse variabler'''
    home = tenant_dir(settings, tenant_id)
    smtp = settings.smtp
    env = base_env(home)
    env.update({
        'FSKINTRA_LOGINTYPE': profile.logintype,
        'FSKINTRA_USERNAME': profile.username,
        'FSKINTRA_PASSWORD': profile.password,
        'FSKINTRA_HOSTNAME': profile.hostname,
        'FSKINTRA_RECIPIENT_EMAIL': ', '.join(profile.recipients),
        'FSKINTRA_STORE_MESSAGES': 'marker',
        'FSKINTRA_SENDER_EMAIL': smtp.sender,
        'FSKINTRA_SMTP_HOST': smtp.host,
        'FSKINTRA_SMTP_PORT': str(smtp.port),
        'FSKINTRA_SMTP_VERIFY_SSL': 'true' if smtp.verify_ssl else 'false',
        'FSKINTRA_HA_TODO': 'off',
        'FSKINTRA_HA_CALENDAR': 'off',
        # fskintra sletter selv cachede sider/billeder efter så mange dage.
        # Browser-tilstanden bevares, så login ikke skal gentages
        'FSKINTRA_PRUNE_CACHE_DAYS': str(settings.cache_retention_days),
        # ... og markeringer af sendte mails, når indholdet ikke har været
        # at se på ForældreIntra i så mange dage
        'FSKINTRA_PRUNE_MARKERS_DAYS': str(settings.marker_retention_days),
    })
    if smtp.user:
        env['FSKINTRA_SMTP_USER'] = smtp.user
        env['FSKINTRA_SMTP_PASSWORD'] = smtp.password
    if profile.ai_key:
        env['FSKINTRA_ANTHROPIC_API_KEY'] = profile.ai_key
        env['FSKINTRA_AI_MODEL'] = profile.ai_model or settings.shared_ai_model
        if profile.ai_base_url:
            env['FSKINTRA_AI_BASE_URL'] = profile.ai_base_url
    return env


def _limit(pid, settings):
    '''Ressourcegrænser for den nye proces (efter fork - preexec_fn er ikke
    sikker med tråde)'''
    limits = (
        (resource.RLIMIT_AS, 1536 * 1024 * 1024),
        (resource.RLIMIT_CPU, settings.run_timeout_seconds),
        (resource.RLIMIT_FSIZE, 200 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 256),
    )
    for which, value in limits:
        try:
            resource.prlimit(pid, which, (value, value))
        except (OSError, ValueError, AttributeError):
            pass


def _scrub(text, profile):
    for secret in (profile.password, profile.ai_key):
        if secret and len(secret) >= 4:
            text = text.replace(secret, '[skjult]')
    return text


def run(settings, tenant_id, profile, kind, lock_timeout=-1):
    '''Kør fskintra. Returnerer None hvis der allerede kører noget for den
    samme forælder (og lock_timeout udløber)'''
    lock = tenant_lock(tenant_id)
    if not lock.acquire(timeout=lock_timeout):
        return None
    try:
        return _run(settings, tenant_id, profile, kind)
    finally:
        lock.release()


def _run(settings, tenant_id, profile, kind):
    env = build_env(settings, tenant_id, profile)
    args = list(KINDS[kind])
    if kind != 'check' and profile.sections:
        args += ['--section', ','.join(profile.sections)]
    cmd = [sys.executable, settings.fskintra_path] + args

    started = time.time()
    # Fast kommando (sys.executable + fskintra-stien), ingen shell, og
    # argumenterne kommer fra KINDS og en valideret afsnitsliste
    proc = subprocess.Popen(  # nosec B603
        cmd, cwd=env['HOME'], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, umask=0o077, close_fds=True)
    _limit(proc.pid, settings)
    timeout = settings.run_timeout_seconds
    if kind == 'check':
        timeout = min(timeout, CHECK_TIMEOUT)
    try:
        out, err = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
        code = EXIT_TIMEOUT
        err += b'\n[afbrudt efter %d sekunder]' % timeout

    log_tail = _scrub(err.decode('utf-8', 'replace'), profile)[-LOG_TAIL:]
    return RunResult(kind=kind, started_at=int(started),
                     duration=round(time.time() - started, 1),
                     exit_code=code,
                     stdout=_scrub(out.decode('utf-8', 'replace'), profile),
                     log_tail=log_tail)


PURGE_TIMEOUT = 60


def purge_files(settings, home):
    '''Slet alt, fskintra har hentet og gemt for forælderen: cache (også
    browser-tilstanden), ugeplaner, markeringer af sendte mails og
    overblikket. True hvis noget blev slettet.

    fskintra sletter selv (--purge) og kender katalogerne. Flaget kræver
    ikke login, så det virker også for en forælder uden gemt kodeord'''
    if not os.path.isdir(home):
        return False
    cmd = [sys.executable, settings.fskintra_path, '--purge']
    try:
        # Fast kommando uden shell, se _run
        proc = subprocess.run(  # nosec B603
            cmd, cwd=home, env=base_env(home), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=PURGE_TIMEOUT, umask=0o077,
            close_fds=True)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning('Kunne ikke slette hentede data: %r', e)
        return False
    if proc.returncode != 0:
        log.warning('Sletning af hentede data gav exit %d', proc.returncode)
    try:
        lines = proc.stdout.decode('utf-8', 'replace').strip().splitlines()
        return bool(json.loads(lines[-1]).get('purged'))
    except (ValueError, IndexError, AttributeError):
        return False

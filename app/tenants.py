"""Forældre ("tenants"): oprettelse, indstillinger, modtagere, log og
sletning. Alle funktioner tager tenant_id eksplicit og alle SQL-forespørgsler
er afgrænset til den ene forælder."""

import hashlib
import json
import re
import secrets
import shutil
import uuid

from . import db
from .crypto import DecryptError
from .db import now

TENANT_ID_RE = re.compile(r'^[0-9a-f]{32}$')
# secrets.token_urlsafe(32)
ICS_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{43}$')
# Kun ASCII: mange SMTP-servere understøtter ikke SMTPUTF8
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+"
                      r'@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$')
MAX_RECIPIENTS = 3
KEEP_RUNS = 10
KEEP_AUDIT = 50


def normalize_email(value):
    '''Lovlig mailadresse i små bogstaver - eller None'''
    value = (value or '').strip().lower()
    if len(value) > 254 or not EMAIL_RE.match(value):
        return None
    return value


def create(conn, email):
    tid = uuid.uuid4().hex
    t = now()
    with db.Tx(conn):
        conn.execute('INSERT INTO tenants (id, email, created_at) '
                     'VALUES (?, ?, ?)', (tid, email, t))
        # Login-mailen er bevist ejet via magic link - den er straks modtager
        conn.execute('INSERT INTO recipients (tenant_id, email, approved_at, '
                     'confirmed_at, created_at) VALUES (?, ?, ?, ?, ?)',
                     (tid, email, t, t, t))
    audit(conn, tid, 'Konto oprettet')
    return tid


def get(conn, tenant_id):
    if not TENANT_ID_RE.match(tenant_id or ''):
        return None
    return conn.execute('SELECT * FROM tenants WHERE id = ?',
                        (tenant_id,)).fetchone()


def get_by_email(conn, email):
    return conn.execute('SELECT * FROM tenants WHERE email = ?',
                        (email,)).fetchone()


def all_tenants(conn):
    return conn.execute('SELECT * FROM tenants ORDER BY created_at').fetchall()


UPDATABLE = frozenset((
    'status', 'pause_reason', 'logintype', 'hostname', 'username',
    'password_enc', 'children', 'sections', 'ai_mode', 'ai_key_enc',
    'ai_model', 'needs_catchup', 'run_requested', 'fail_count',
    'last_run_at', 'last_exit', 'ics_token_hash', 'ics_token_enc',
    'ics_created_at', 'inactive_warned_at'))


def update(conn, tenant_id, **fields):
    # Kolonnenavne kommer kun fra den faste liste UPDATABLE - værdierne
    # sendes altid som parametre
    if not fields or not set(fields) <= UPDATABLE:
        raise ValueError('ukendte felter: %r' % (set(fields) - UPDATABLE))
    cols = ', '.join('%s = ?' % k for k in sorted(fields))
    conn.execute('UPDATE tenants SET %s WHERE id = ?' % cols,  # nosec B608
                 [fields[k] for k in sorted(fields)] + [tenant_id])


def decrypt_secrets(crypto, tenant):
    '''(ForældreIntra-kodeord, egen AI-nøgle) - tomme strenge hvis ikke sat
    eller hvis de ikke kan dekrypteres (fx forkert MASTER_KEY)'''
    out = []
    for column, field in (('password_enc', 'password'),
                          ('ai_key_enc', 'ai_key')):
        value = tenant[column]
        if not value:
            out.append('')
            continue
        try:
            out.append(crypto.decrypt(value, tenant['id'], field))
        except DecryptError:
            out.append('')
    return tuple(out)


def rotate_keys(conn, crypto):
    '''Genkryptér værdier der er krypteret med MASTER_KEY_OLD'''
    n = 0
    for t in all_tenants(conn):
        for column, field in (('password_enc', 'password'),
                              ('ai_key_enc', 'ai_key'),
                              ('ics_token_enc', 'ics_token')):
            value = t[column]
            if value and crypto.needs_rotation(value):
                try:
                    plain = crypto.decrypt(value, t['id'], field)
                except DecryptError:
                    continue
                update(conn, t['id'],
                       **{column: crypto.encrypt(plain, t['id'], field)})
                n += 1
    return n


# Kalenderlink: et hemmeligt token i adressen giver læseadgang til
# kalenderen uden login (en kalender-app kan ikke logge ind)

def _ics_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def new_ics_token(conn, crypto, tenant_id):
    '''Lav et (nyt) kalenderlink - et tidligere holder op med at virke.

    Opslag sker på en hash, så et læk af databasen ikke giver adgang. Den
    krypterede kopi er kun til at vise linket igen på statussiden'''
    token = secrets.token_urlsafe(32)
    update(conn, tenant_id, ics_token_hash=_ics_hash(token),
           ics_token_enc=crypto.encrypt(token, tenant_id, 'ics_token'),
           ics_created_at=now())
    return token


def clear_ics_token(conn, tenant_id):
    update(conn, tenant_id, ics_token_hash=None, ics_token_enc=None,
           ics_created_at=None)


def get_by_ics_token(conn, token):
    if not ICS_TOKEN_RE.match(token or ''):
        return None
    return conn.execute('SELECT * FROM tenants WHERE ics_token_hash = ?',
                        (_ics_hash(token),)).fetchone()


def ics_token(crypto, tenant):
    '''Tokenet til det aktive kalenderlink - eller tom streng, hvis der
    ikke er noget, eller det ikke kan dekrypteres (fx forkert MASTER_KEY)'''
    if not tenant['ics_token_hash'] or not tenant['ics_token_enc']:
        return ''
    try:
        token = crypto.decrypt(tenant['ics_token_enc'], tenant['id'],
                               'ics_token')
    except DecryptError:
        return ''
    # Den krypterede kopi skal høre til det link, der virker
    if _ics_hash(token) != tenant['ics_token_hash']:
        return ''
    return token


def sections(tenant):
    return [s for s in (tenant['sections'] or '').split(',') if s]


def children(tenant):
    try:
        return json.loads(tenant['children'] or '[]')
    except ValueError:
        return []


# Modtagere

def recipients(conn, tenant_id):
    return conn.execute('SELECT * FROM recipients WHERE tenant_id = ? '
                        'ORDER BY id', (tenant_id,)).fetchall()


def confirmed_recipients(conn, tenant_id):
    return [r['email'] for r in conn.execute(
        'SELECT email FROM recipients WHERE tenant_id = ? AND approved_at IS '
        'NOT NULL AND confirmed_at IS NOT NULL ORDER BY id', (tenant_id,))]


def add_recipient(conn, tenant_id, email):
    '''Tilføj en (endnu ikke godkendt/bekræftet) modtager. Returnerer rækkens
    id, eller None hvis den findes eller der er for mange'''
    if len(recipients(conn, tenant_id)) >= MAX_RECIPIENTS:
        return None
    try:
        cur = conn.execute('INSERT INTO recipients (tenant_id, email, '
                           'created_at) VALUES (?, ?, ?)',
                           (tenant_id, email, now()))
    except db.sqlite3.IntegrityError:
        return None
    return cur.lastrowid


def get_recipient(conn, tenant_id, recipient_id):
    return conn.execute('SELECT * FROM recipients WHERE tenant_id = ? AND '
                        'id = ?', (tenant_id, recipient_id)).fetchone()


_SET_RECIPIENT = {
    'approved_at': 'UPDATE recipients SET approved_at = ? WHERE tenant_id = ? '
                   'AND id = ?',
    'confirmed_at': 'UPDATE recipients SET confirmed_at = ? WHERE tenant_id = '
                    '? AND id = ?',
}


def set_recipient(conn, tenant_id, recipient_id, column):
    conn.execute(_SET_RECIPIENT[column], (now(), tenant_id, recipient_id))


def remove_recipient(conn, tenant_id, recipient_id, login_email):
    cur = conn.execute(
        'DELETE FROM recipients WHERE tenant_id = ? AND id = ? AND email != ?',
        (tenant_id, recipient_id, login_email))
    return cur.rowcount == 1


# Log over kørsler og hændelser

def audit(conn, tenant_id, event):
    conn.execute('INSERT INTO audit (tenant_id, ts, event) VALUES (?, ?, ?)',
                 (tenant_id, now(), event))
    conn.execute('DELETE FROM audit WHERE tenant_id = ? AND id NOT IN (SELECT '
                 'id FROM audit WHERE tenant_id = ? ORDER BY id DESC LIMIT ?)',
                 (tenant_id, tenant_id, KEEP_AUDIT))


def audit_log(conn, tenant_id, limit=20):
    return conn.execute('SELECT * FROM audit WHERE tenant_id = ? ORDER BY id '
                        'DESC LIMIT ?', (tenant_id, limit)).fetchall()


def record_run(conn, tenant_id, result):
    conn.execute('INSERT INTO runs (tenant_id, kind, started_at, duration, '
                 'exit_code, log_tail) VALUES (?, ?, ?, ?, ?, ?)',
                 (tenant_id, result.kind, result.started_at, result.duration,
                  result.exit_code, result.log_tail))
    conn.execute('DELETE FROM runs WHERE tenant_id = ? AND id NOT IN (SELECT '
                 'id FROM runs WHERE tenant_id = ? ORDER BY id DESC LIMIT ?)',
                 (tenant_id, tenant_id, KEEP_RUNS))


def record_failure(conn, tenant_id):
    '''En kørsel stoppede med en exception i stedet for et resultat. Tæl
    fejlen og flyt forælderen bagerst i køen, så den ikke blokerer'''
    conn.execute('UPDATE tenants SET last_run_at = ?, fail_count = '
                 'fail_count + 1 WHERE id = ?', (now(), tenant_id))


def runs(conn, tenant_id, limit=KEEP_RUNS):
    return conn.execute('SELECT * FROM runs WHERE tenant_id = ? ORDER BY id '
                        'DESC LIMIT ?', (tenant_id, limit)).fetchall()


def export(conn, tenant_id):
    '''Alle data om en forælder - uden hemmeligheder'''
    t = get(conn, tenant_id)
    return {
        'konto': {
            'email': t['email'],
            'oprettet': t['created_at'],
            'status': t['status'],
            'pause_aarsag': t['pause_reason'],
            'skole': t['hostname'],
            'logintype': t['logintype'],
            'brugernavn': t['username'],
            'kodeord_gemt': bool(t['password_enc']),
            'boern': children(t),
            'afsnit': sections(t),
            'ai': t['ai_mode'],
            'egen_ai_noegle_gemt': bool(t['ai_key_enc']),
            'ai_model': t['ai_model'],
            # Selve linket er en hemmelighed og er ikke med
            'kalenderlink': bool(t['ics_token_hash']),
            'kalenderlink_oprettet': t['ics_created_at'],
        },
        'modtagere': [{'email': r['email'],
                       'godkendt': r['approved_at'],
                       'bekraeftet': r['confirmed_at']}
                      for r in recipients(conn, tenant_id)],
        'koersler': [{'type': r['kind'], 'start': r['started_at'],
                      'varighed': r['duration'], 'exit': r['exit_code'],
                      'log': r['log_tail']}
                     for r in runs(conn, tenant_id)],
        'haendelser': [{'tid': a['ts'], 'haendelse': a['event']}
                       for a in audit_log(conn, tenant_id, KEEP_AUDIT)],
    }


def delete(conn, settings, tenant_id):
    '''Slet en forælder helt: rækker (cascade) og alle filer'''
    if not TENANT_ID_RE.match(tenant_id or ''):
        raise ValueError('ugyldigt tenant-id')
    t = get(conn, tenant_id)
    with db.Tx(conn):
        if t is not None:
            # Tilmeldings-tokens er ikke knyttet til et tenant-id
            conn.execute('DELETE FROM tokens WHERE email = ?', (t['email'],))
        conn.execute('DELETE FROM tenants WHERE id = ?', (tenant_id,))
    shutil.rmtree('%s/%s' % (settings.tenants_dir, tenant_id),
                  ignore_errors=True)

"""Engangs-tokens (magic links), server-side sessions og rate limiting.

Der gemmes kun SHA-256 af tokens og sessions - et læk af databasen giver
ikke adgang. Rate limits gemmer kun en hash af IP/mail."""

import hashlib
import re
import secrets

from .db import now

TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{40,64}$')

TOKEN_TTL = {
    'signup': 15 * 60,
    'login': 15 * 60,
    'admin_login': 15 * 60,
    'approve_recipient': 48 * 3600,
    'confirm_recipient': 48 * 3600,
}

SESSION_IDLE = 30 * 86400
SESSION_ABSOLUTE = 90 * 86400
ADMIN_SESSION_IDLE = 3600
ADMIN_SESSION_ABSOLUTE = 12 * 3600


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def new_token(conn, purpose, email, tenant_id=None, payload=None):
    token = secrets.token_urlsafe(32)
    t = now()
    conn.execute(
        'INSERT INTO tokens (hash, purpose, email, tenant_id, payload, '
        'created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
        (_hash(token), purpose, email, tenant_id, payload, t,
         t + TOKEN_TTL[purpose]))
    return token


def peek_token(conn, token, purposes):
    '''Gyldigt token? Bruger det IKKE (GET må ikke forbruge et link - mail-
    scannere følger links automatisk)'''
    if not TOKEN_RE.match(token or ''):
        return None
    row = conn.execute(
        'SELECT * FROM tokens WHERE hash = ? AND used_at IS NULL AND '
        'expires_at > ?', (_hash(token), now())).fetchone()
    if row is None or row['purpose'] not in purposes:
        return None
    return row


def consume_token(conn, token, purposes):
    '''Brug et token atomisk - kun ét kald kan lykkes'''
    if peek_token(conn, token, purposes) is None:
        return None
    h = _hash(token)
    cur = conn.execute(
        'UPDATE tokens SET used_at = ? WHERE hash = ? AND used_at IS NULL '
        'AND expires_at > ?', (now(), h, now()))
    if cur.rowcount != 1:
        return None
    return conn.execute('SELECT * FROM tokens WHERE hash = ?',
                        (h,)).fetchone()


def create_session(conn, tenant_id=None, is_admin=False):
    token = secrets.token_urlsafe(32)
    t = now()
    conn.execute(
        'INSERT INTO sessions (hash, tenant_id, is_admin, created_at, '
        'last_seen) VALUES (?, ?, ?, ?, ?)',
        (_hash(token), tenant_id, 1 if is_admin else 0, t, t))
    return token


def load_session(conn, token, admin=False):
    if not TOKEN_RE.match(token or ''):
        return None
    h = _hash(token)
    row = conn.execute('SELECT * FROM sessions WHERE hash = ?',
                       (h,)).fetchone()
    if row is None or bool(row['is_admin']) != admin:
        return None
    idle, absolute = ((ADMIN_SESSION_IDLE, ADMIN_SESSION_ABSOLUTE) if admin
                      else (SESSION_IDLE, SESSION_ABSOLUTE))
    t = now()
    if t - row['last_seen'] > idle or t - row['created_at'] > absolute:
        conn.execute('DELETE FROM sessions WHERE hash = ?', (h,))
        return None
    if t - row['last_seen'] > 300:
        conn.execute('UPDATE sessions SET last_seen = ? WHERE hash = ?',
                     (t, h))
    return row


def delete_session(conn, token):
    if token:
        conn.execute('DELETE FROM sessions WHERE hash = ?', (_hash(token),))


def delete_all_sessions(conn, tenant_id):
    conn.execute('DELETE FROM sessions WHERE tenant_id = ?', (tenant_id,))


def rate_limited(conn, kind, value, limit, window):
    '''True hvis grænsen er nået. Ellers tælles forsøget med'''
    key = _hash('%s:%s' % (kind, value))[:32]
    t = now()
    conn.execute('DELETE FROM rate_limits WHERE key = ? AND ts <= ?',
                 (key, t - window))
    n = conn.execute('SELECT COUNT(*) FROM rate_limits WHERE key = ?',
                     (key,)).fetchone()[0]
    if n >= limit:
        return True
    conn.execute('INSERT INTO rate_limits (key, ts) VALUES (?, ?)', (key, t))
    return False


def housekeeping(conn):
    t = now()
    conn.execute('DELETE FROM tokens WHERE expires_at < ?', (t - 86400,))
    conn.execute('DELETE FROM rate_limits WHERE ts < ?', (t - 86400,))
    conn.execute('DELETE FROM sessions WHERE (is_admin = 0 AND (last_seen < ?'
                 ' OR created_at < ?)) OR (is_admin = 1 AND (last_seen < ? '
                 'OR created_at < ?))',
                 (t - SESSION_IDLE, t - SESSION_ABSOLUTE,
                  t - ADMIN_SESSION_IDLE, t - ADMIN_SESSION_ABSOLUTE))

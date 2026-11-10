"""SQLite på Fly-volumet. Én fil, WAL, foreign keys og simple migrationer
via PRAGMA user_version. Alle forespørgsler bruger parametre - aldrig
strengsammensætning."""

import os
import sqlite3
import threading
import time

MIGRATIONS = [
    # 1: første version
    '''
    CREATE TABLE tenants (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        pause_reason TEXT,
        logintype TEXT NOT NULL DEFAULT 'alm',
        hostname TEXT,
        username TEXT,
        password_enc TEXT,
        children TEXT,
        sections TEXT NOT NULL DEFAULT '',
        ai_mode TEXT NOT NULL DEFAULT 'off',
        ai_key_enc TEXT,
        ai_model TEXT,
        needs_catchup INTEGER NOT NULL DEFAULT 1,
        run_requested INTEGER NOT NULL DEFAULT 0,
        fail_count INTEGER NOT NULL DEFAULT 0,
        last_run_at INTEGER,
        last_exit INTEGER
    );
    CREATE TABLE recipients (
        id INTEGER PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        email TEXT NOT NULL,
        approved_at INTEGER,
        confirmed_at INTEGER,
        created_at INTEGER NOT NULL,
        UNIQUE (tenant_id, email)
    );
    CREATE TABLE tokens (
        hash TEXT PRIMARY KEY,
        purpose TEXT NOT NULL,
        email TEXT NOT NULL,
        tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
        payload TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        used_at INTEGER
    );
    CREATE TABLE sessions (
        hash TEXT PRIMARY KEY,
        tenant_id TEXT REFERENCES tenants(id) ON DELETE CASCADE,
        is_admin INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        duration REAL,
        exit_code INTEGER,
        log_tail TEXT
    );
    CREATE INDEX runs_tenant ON runs (tenant_id, started_at);
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY,
        tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        ts INTEGER NOT NULL,
        event TEXT NOT NULL
    );
    CREATE TABLE rate_limits (
        key TEXT NOT NULL,
        ts INTEGER NOT NULL
    );
    CREATE INDEX rate_limits_key ON rate_limits (key, ts);
    ''',
    # 2: engangskoder (login fra appen på hjemmeskærmen) tæller forsøg
    '''
    ALTER TABLE tokens ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
    ''',
    # 3: hemmeligt kalenderlink (ICS). Hash til opslag, og en krypteret kopi,
    # så linket kan vises igen på statussiden
    '''
    ALTER TABLE tenants ADD COLUMN ics_token_hash TEXT;
    ALTER TABLE tenants ADD COLUMN ics_token_enc TEXT;
    ALTER TABLE tenants ADD COLUMN ics_created_at INTEGER;
    CREATE UNIQUE INDEX tenants_ics_token ON tenants (ics_token_hash);
    ''',
    # 4: hvornår forælderen sidst fik varsel om, at en inaktiv konto slettes
    '''
    ALTER TABLE tenants ADD COLUMN inactive_warned_at INTEGER;
    ''',
    # 5: fælles cache for AI-svar (se aiproxy.py). Nøglen er SHA-256 af
    # forespørgslen - ingen kobling til en forælder
    '''
    CREATE TABLE ai_cache (
        key TEXT PRIMARY KEY,
        response TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    ''',
]

_local = threading.local()


def connect(path):
    '''Én forbindelse pr. tråd og databasefil'''
    conns = getattr(_local, 'conns', None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(path)
    if conn is None:
        dn = os.path.dirname(path)
        if dn:
            os.makedirs(dn, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA busy_timeout = 10000')
        conn.execute('PRAGMA secure_delete = ON')
        conns[path] = conn
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


def close_all():
    for conn in getattr(_local, 'conns', {}).values():
        conn.close()
    _local.conns = {}


def migrate(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript('BEGIN;' + sql + 'PRAGMA user_version = %d;'
                           'COMMIT;' % i)


def now():
    return int(time.time())


class Tx:
    '''with Tx(conn): ... - en eksplicit transaktion (autocommit ellers)'''

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute('BEGIN IMMEDIATE')
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute('COMMIT')
        else:
            self.conn.execute('ROLLBACK')
        return False

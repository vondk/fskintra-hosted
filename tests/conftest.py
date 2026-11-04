import base64
import dataclasses
import os
import re
import sys

import pytest
from flask.testing import FlaskClient

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app import db, settings as settings_mod  # noqa: E402
from app.admin import create_admin_app  # noqa: E402
from app.context import Context  # noqa: E402
from app.web import create_public_app  # noqa: E402

FAKE = os.path.join(REPO, 'tests', 'fake_fskintra.py')
PUBLIC = 'https://hosted.example'
HOST = 'aaskolen.m.skoleintra.dk'
INVITE = 'klasse-3b-hemmelig-kode'
MASTER_KEY = base64.b64encode(b'k' * 32).decode()


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return True

    def last_link(self, to=None):
        for t, _s, body in reversed(self.sent):
            if to is None or t == to:
                m = re.search(r'https?://\S+/auth/([A-Za-z0-9_-]+)', body)
                if m:
                    return m.group(1)
        raise AssertionError('ingen link sendt til %r' % to)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith('FSKINTRA_'):
            monkeypatch.delenv(name)
    values = {
        'DATA_DIR': str(tmp_path / 'data'),
        'PUBLIC_URL': PUBLIC,
        'ADMIN_URL': 'http://localhost:9090',
        'ADMIN_EMAIL': 'admin@example.net',
        'INVITE_CODE': INVITE,
        'MASTER_KEY': MASTER_KEY,
        'ALLOWED_SCHOOL_HOSTS': HOST,
        'FSKINTRA_SENDER_EMAIL': 'afsender@example.net',
        'FSKINTRA_SMTP_HOST': 'smtp.example.net',
        'FSKINTRA_SMTP_PORT': '587',
        'FSKINTRA_SMTP_USER': 'smtpbruger',
        'FSKINTRA_SMTP_PASSWORD': 'smtpkode',
        'FSKINTRA_ANTHROPIC_API_KEY': 'sk-ant-faelles-' + 'x' * 30,
        'FSKINTRA_PATH': FAKE,
        'TRUST_FLY_CLIENT_IP': '0',
        # Hemmeligheder på serveren, der aldrig må nå fskintra
        'FSKINTRA_HA_URL': 'http://ha.local:8123',
        'FSKINTRA_MQTT_HOST': '10.0.0.2',
        'AWS_SECRET_ACCESS_KEY': 'må-ikke-arves',
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return values


@pytest.fixture
def ctx(env):
    db.close_all()
    c = Context(settings_mod.load(), mailer=FakeMailer(),
                ai_key_validator=lambda key: key.endswith('ok'))
    yield c
    db.close_all()


def with_settings(ctx, **changes):
    ctx.settings = dataclasses.replace(ctx.settings, **changes)
    return ctx


def make_client(app, scheme='https', host='hosted.example'):
    base = '%s://%s' % (scheme, host)

    class Client(FlaskClient):
        def open(self, *args, **kw):
            kw.setdefault('base_url', base)
            return super().open(*args, **kw)

    app.test_client_class = Client
    return app.test_client()


def new_client(client):
    '''Endnu en browser mod samme app'''
    return make_client(client.application)


@pytest.fixture
def client(ctx):
    app = create_public_app(ctx)
    app.testing = True
    return make_client(app)


@pytest.fixture
def admin_client(ctx):
    app = create_admin_app(ctx)
    app.testing = True
    return make_client(app, 'http', 'localhost:9090')


def csrf(client, cookie='__Host-csrf', domain='hosted.example'):
    c = client.get_cookie(cookie, domain=domain)
    if c is None:
        client.get('/healthz')
        c = client.get_cookie(cookie, domain=domain)
    return c.value


def post(client, url, data=None, **kw):
    data = dict(data or {})
    data.setdefault('csrf', csrf(client))
    return client.post(url, data=data, **kw)


def signup(client, ctx, email='foraelder@example.net'):
    '''Tilmeld og log ind. Returnerer tenant-id'''
    r = post(client, '/signup', {'email': email, 'invite': INVITE})
    assert r.status_code == 200
    token = ctx.mailer.last_link(email)
    r = post(client, '/auth/' + token)
    assert r.status_code == 303
    conn = ctx.db()
    return conn.execute('SELECT id FROM tenants WHERE email = ?',
                        (email,)).fetchone()['id']


def save_settings(client, **override):
    data = {'hostname': HOST, 'logintype': 'alm', 'username': 'petjen',
            'password': 'Rigtig-Kode-123', 'ai_mode': 'off',
            'ai_model': 'claude-haiku-5-5', 'section_pln': 'on',
            'section_msg': 'on'}
    data.update(override)
    data = {k: v for k, v in data.items() if v is not None}
    return post(client, '/settings', data)

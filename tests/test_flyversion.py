import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import flyversion, settings as settings_mod
from app.admin import create_admin_app
from app.web import create_public_app
from conftest import make_client, with_settings

FLY_ENV = {'FLY_APP_NAME': 'fskintra-hosted', 'FLY_MACHINE_ID': '287e62'}


class _UnixHTTPServer(HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)

    def get_request(self):
        sock, _ = self.socket.accept()
        return sock, ('local', 0)


def _serve(path, status=200, body=None):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = _UnixHTTPServer(path, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def sock_path(tmp_path):
    # AF_UNIX-stier må højst være ~100 tegn, tmp_path kan være længere
    path = os.path.join('/tmp', 'flyapi-%d-%s.sock' % (os.getpid(),
                                                       tmp_path.name))
    yield path
    if os.path.exists(path):
        os.unlink(path)


def test_reads_release_version_from_machine_metadata(sock_path):
    server, seen = _serve(sock_path, body={'fly_release_version': '14',
                                           'fly_process_group': 'app'})
    try:
        assert flyversion.release_version(sock_path, FLY_ENV) == 'v14'
    finally:
        server.shutdown()
    assert seen == ['/v1/apps/fskintra-hosted/machines/287e62/metadata']


@pytest.mark.parametrize('status,body', [
    (500, {'fly_release_version': '14'}),
    (200, {}),
    (200, {'fly_release_version': '<script>'}),
])
def test_bad_answer_gives_no_version(sock_path, status, body):
    server, _ = _serve(sock_path, status, body)
    try:
        assert flyversion.release_version(sock_path, FLY_ENV) == ''
    finally:
        server.shutdown()


def test_outside_fly_gives_no_version(sock_path):
    assert flyversion.release_version(sock_path, FLY_ENV) == ''
    assert flyversion.release_version(sock_path, {}) == ''


def test_dead_socket_gives_no_version(sock_path):
    s = socket.socket(socket.AF_UNIX)
    s.bind(sock_path)  # findes, men ingen lytter
    try:
        assert flyversion.release_version(sock_path, FLY_ENV) == ''
    finally:
        s.close()


def test_version_in_footer(ctx):
    with_settings(ctx, version='v14')
    public = make_client(create_public_app(ctx))
    assert b'class="app-version" title="Version">v14<' in public.get('/').data
    admin = make_client(create_admin_app(ctx), 'http', 'localhost:9090')
    assert b'>v14<' in admin.get('/admin/login').data


def test_app_version_from_env(env, monkeypatch):
    monkeypatch.setenv('APP_VERSION', ' v14 ')
    assert settings_mod.load().version == 'v14'


def test_no_version_no_footer_entry(client):
    assert b'app-version' not in client.get('/').data

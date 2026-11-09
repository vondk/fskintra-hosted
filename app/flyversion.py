"""Fly-releasenummeret (det samme som `fly releases` viser, fx v14), så
siderne kan vise hvilken version der kører uden at nogen bumper et tal.

Nummeret ligger i maskinens metadata, som kun kan læses via /.fly/api.
Socket'en ejes af root, så entrypoint.sh kører `python3 -m app.flyversion`
inden rettighederne droppes og giver svaret videre i APP_VERSION."""

import http.client
import json
import os
import socket
import sys

SOCKET = '/.fly/api'


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('flaps', timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def release_version(path=SOCKET, env=None, timeout=2):
    '''"v14" på Fly, ellers "" (lokalt, i tests, hvis API'et fejler).
    Må aldrig forhindre appen i at starte'''
    env = os.environ if env is None else env
    app, machine = env.get('FLY_APP_NAME'), env.get('FLY_MACHINE_ID')
    if not (app and machine and os.path.exists(path)):
        return ''
    conn = _UnixConnection(path, timeout)
    try:
        conn.request('GET', '/v1/apps/%s/machines/%s/metadata'
                     % (app, machine))
        resp = conn.getresponse()
        if resp.status != 200:
            return ''
        version = str(json.loads(resp.read()).get('fly_release_version', ''))
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        return ''
    finally:
        conn.close()
    return 'v' + version if version.isdigit() else ''


if __name__ == '__main__':
    sys.stdout.write(release_version())

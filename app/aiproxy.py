"""Lokal proxy foran Claude API med en cache, der deles af alle forældre.

Ugeplanen er den samme for alle børn i en klasse, og fskintra sender den
til AI uden barnets navn. To forældre i samme klasse sender derfor præcis
den samme forespørgsel, og den anden får svaret herfra uden et nyt kald.

* Nøglen er SHA-256 af forespørgslen (model, prompt og svarformat). Der er
  ingen kobling mellem forældre: man får kun et svar, hvis man selv har den
  identiske tekst.
* Cachen spørges først. Kun ved et miss bruges forælderens nøgle - egen
  eller den fælles.
* fskintra får hverken den fælles eller forælderens egen nøgle, men et
  engangstoken, der kun gælder under kørslen og kun findes i hukommelsen.
* Lytter kun på 127.0.0.1.
"""

import contextlib
import hashlib
import json
import logging
import secrets
import threading

import httpx2
from flask import Flask, Response, request
from werkzeug.serving import make_server

from .db import now

log = logging.getLogger(__name__)

UPSTREAM = 'https://api.anthropic.com/v1/messages'
MAX_BODY = 1024 * 1024
# En ugeplan lever en uge, og nøglen skifter selv, når indholdet gør
TTL = 14 * 86400
# Lidt længere end fskintras egen timeout (120 s)
TIMEOUT = 130.0
FORWARD_REQUEST = ('content-type', 'anthropic-version', 'anthropic-beta')
FORWARD_RESPONSE = ('retry-after', 'request-id')


def _send(query, headers, raw):
    '''Kald Claude API. Returnerer (status, headers, body)'''
    r = httpx2.post(UPSTREAM, params=query, headers=headers, content=raw,
                    timeout=TIMEOUT)
    return r.status_code, r.headers, r.content


def _error(status, kind, message):
    body = {'type': 'error', 'error': {'type': kind, 'message': message}}
    return Response(json.dumps(body), status=status,
                    content_type='application/json')


def _credentials(headers):
    '''(header, præfiks, token) fra en forespørgsel. Anthropics klient
    sender nøglen som Bearer-token, ældre versioner i x-api-key'''
    value = headers.get('authorization', '')
    if value.startswith('Bearer '):
        return 'authorization', 'Bearer ', value[len('Bearer '):]
    return 'x-api-key', '', headers.get('x-api-key', '')


def _cacheable(status, body):
    '''Kun hele, almindelige svar gemmes - ikke fejl, afvisninger eller
    afkortede svar, som fskintra alligevel kasserer'''
    if status != 200:
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get('type') == 'message' and \
        data.get('stop_reason') == 'end_turn'


def prune(conn):
    conn.execute('DELETE FROM ai_cache WHERE created_at < ?', (now() - TTL,))


class AiProxy:
    def __init__(self, db, send=_send):
        self._db = db
        self._send = send
        self._sessions = {}
        self._guard = threading.Lock()
        # Ét kald til API'et ad gangen: planlæggeren kører alligevel én
        # forælder ad gangen, og låsen sikrer, at to samtidige kørsler med
        # samme ugeplan ikke begge betaler for svaret
        self._upstream = threading.Lock()
        # Sættes af start(). Uden adresse bruger kørslerne nøglen direkte
        self.url = None
        self.app = self._create_app()

    def start(self, port):
        server = make_server('127.0.0.1', port, self.app, threaded=True)
        threading.Thread(target=server.serve_forever, name='aiproxy',
                         daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % server.server_port
        return server

    @contextlib.contextmanager
    def session(self, key):
        '''Et token, der kan bruges i stedet for key, så længe blokken
        varer - eller None, hvis proxyen ikke kører'''
        if not self.url or not key:
            yield None
            return
        token = 'proxy-' + secrets.token_urlsafe(32)
        with self._guard:
            self._sessions[token] = key
        try:
            yield token
        finally:
            with self._guard:
                self._sessions.pop(token, None)

    def _lookup(self, cache_key):
        row = self._db().execute(
            'SELECT response FROM ai_cache WHERE key = ? AND created_at >= ?',
            (cache_key, now() - TTL)).fetchone()
        return row['response'] if row else None

    def _create_app(self):
        app = Flask(__name__)
        app.config['MAX_CONTENT_LENGTH'] = MAX_BODY

        @app.post('/v1/messages')
        def messages():
            header, prefix, token = _credentials(request.headers)
            with self._guard:
                key = self._sessions.get(token)
            if not key:
                return _error(401, 'authentication_error', 'ukendt token')
            raw = request.get_data()
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict) or body.get('stream'):
                return _error(400, 'invalid_request_error',
                              'kun JSON uden streaming')
            headers = {h: request.headers[h] for h in FORWARD_REQUEST
                       if h in request.headers}
            cache_key = hashlib.sha256(json.dumps(
                [body, headers.get('anthropic-beta', '')], sort_keys=True,
                ensure_ascii=False).encode('utf-8')).hexdigest()

            cached = self._lookup(cache_key)
            if cached is None:
                with self._upstream:
                    cached = self._lookup(cache_key)
                    if cached is None:
                        # Nøglen sendes videre på samme måde, som tokenet kom
                        headers[header] = prefix + key
                        return self._forward(cache_key, headers, raw)
            log.info('ai-cache hit nøgle=%s', cache_key[:8])
            return Response(cached, content_type='application/json')

        return app

    def _forward(self, cache_key, headers, raw):
        try:
            status, resp_headers, content = self._send(
                request.args.to_dict(), headers, raw)
        except httpx2.HTTPError as e:
            log.warning('ai-cache: API-kald fejlede: %s', type(e).__name__)
            return _error(502, 'api_error', 'ingen forbindelse til API')
        saved = _cacheable(status, content)
        if saved:
            self._db().execute(
                'INSERT OR REPLACE INTO ai_cache (key, response, created_at) '
                'VALUES (?, ?, ?)',
                (cache_key, content.decode('utf-8'), now()))
        log.info('ai-cache miss nøgle=%s status=%d gemt=%s', cache_key[:8],
                 status, 'ja' if saved else 'nej')
        out = {h: resp_headers[h] for h in FORWARD_RESPONSE
               if h in resp_headers}
        return Response(content, status=status, headers=out,
                        content_type=resp_headers.get('content-type',
                                                      'application/json'))

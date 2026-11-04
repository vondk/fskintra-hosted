"""Fælles sikkerhed for den offentlige app og admin-appen: CSRF (double
submit cookie + Origin-tjek) og security headers på alle svar."""

import datetime
import hmac
import secrets

from flask import abort, g, request

from .auth import TOKEN_RE

CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def _origin(url):
    scheme, _, rest = url.partition('://')
    return '%s://%s' % (scheme, rest.split('/', 1)[0])


def install(app, csrf_cookie, secure, expected_url):
    expected_origin = _origin(expected_url)

    @app.before_request
    def _csrf():
        token = request.cookies.get(csrf_cookie, '')
        g.csrf_new = not TOKEN_RE.match(token)
        if g.csrf_new:
            token = secrets.token_urlsafe(32)
        g.csrf = token
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('Origin')
            if origin is not None and origin != expected_origin:
                abort(400)
            sent = request.form.get('csrf', '')
            if g.csrf_new or not hmac.compare_digest(sent, token):
                abort(400)

    @app.after_request
    def _headers(resp):
        if getattr(g, 'csrf_new', False):
            resp.set_cookie(csrf_cookie, g.csrf, secure=secure, httponly=True,
                            samesite='Lax', path='/')
        h = resp.headers
        h['Content-Security-Policy'] = CSP
        h['X-Content-Type-Options'] = 'nosniff'
        h['X-Frame-Options'] = 'DENY'
        h['Referrer-Policy'] = 'no-referrer'
        h['Permissions-Policy'] = ('camera=(), microphone=(), geolocation=(), '
                                   'payment=(), usb=()')
        h['Cross-Origin-Opener-Policy'] = 'same-origin'
        h['Cross-Origin-Resource-Policy'] = 'same-origin'
        if secure:
            h['Strict-Transport-Security'] = ('max-age=63072000; '
                                              'includeSubDomains')
        if not request.path.startswith('/static/'):
            h['Cache-Control'] = 'no-store'
        return resp

    app.jinja_env.globals['csrf_token'] = lambda: g.csrf
    app.jinja_env.filters['dt'] = format_ts


def format_ts(ts):
    '''Unix-tid som dansk lokal tid, fx 04.11.2026 13:05'''
    if not ts:
        return '-'
    return datetime.datetime.fromtimestamp(int(ts)).strftime('%d.%m.%Y %H:%M')

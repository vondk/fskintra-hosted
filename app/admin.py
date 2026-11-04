"""Admin-app. Lytter KUN på ADMIN_PORT, som ikke er eksponeret via Fly-
proxyen - den nås kun via `fly proxy 9090` (WireGuard). Kræver desuden et
magic link sendt til ADMIN_EMAIL. Viser aldrig dekrypterede hemmeligheder."""

import functools
import hmac
import os

from flask import Flask, Response, abort, g, redirect, render_template, request

from . import auth, runner, security, tenants

SESSION_COOKIE = 'asid'


def create_admin_app(ctx):
    base = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, template_folder=os.path.join(base, 'templates'),
                static_folder=os.path.join(base, 'static'))
    app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
    # Trafikken går gennem fly proxy til localhost - derfor ikke Secure
    secure = ctx.settings.admin_url.startswith('https://')
    security.install(app, csrf_cookie='acsrf', secure=secure,
                     expected_url=ctx.settings.admin_url)
    settings = ctx.settings

    def conn():
        return ctx.db()

    @app.before_request
    def _load():
        g.admin = auth.load_session(conn(), request.cookies.get(
            SESSION_COOKIE, ''), admin=True) is not None

    def admin_required(view):
        @functools.wraps(view)
        def wrapper(*a, **kw):
            if not g.admin:
                return redirect('/admin/login', 302)
            return view(*a, **kw)
        return wrapper

    @app.get('/healthz')
    def healthz():
        return Response('ok', mimetype='text/plain')

    @app.get('/')
    def root():
        return redirect('/admin', 302)

    @app.get('/admin/login')
    def login_page():
        return render_template('admin/login.html')

    @app.post('/admin/login')
    def login():
        email = (request.form.get('email') or '').strip().lower()
        if auth.rate_limited(conn(), 'admin-login', 'all', 5, 3600):
            return render_template('admin/login.html', sent=True), 429
        if hmac.compare_digest(email.encode('utf-8'),
                               settings.admin_email.encode('utf-8')):
            token = auth.new_token(conn(), 'admin_login', email)
            ctx.mailer.send(email, 'fskintra admin: log ind',
                            'Log ind (virker i 15 minutter):\n%s/admin/auth/'
                            '%s\n' % (settings.admin_url, token))
        return render_template('admin/login.html', sent=True)

    @app.get('/admin/auth/<token>')
    def auth_confirm(token):
        if auth.peek_token(conn(), token, ('admin_login',)) is None:
            abort(404)
        return render_template('auth_confirm.html', purpose='admin_login',
                               email=settings.admin_email)

    @app.post('/admin/auth/<token>')
    def auth_use(token):
        row = auth.consume_token(conn(), token, ('admin_login',))
        if row is None or row['email'] != settings.admin_email:
            abort(404)
        sid = auth.create_session(conn(), None, is_admin=True)
        resp = redirect('/admin', 303)
        resp.set_cookie(SESSION_COOKIE, sid, secure=secure, httponly=True,
                        samesite='Strict', path='/',
                        max_age=auth.ADMIN_SESSION_ABSOLUTE)
        return resp

    @app.post('/admin/logout')
    def logout():
        auth.delete_session(conn(), request.cookies.get(SESSION_COOKIE, ''))
        resp = redirect('/admin/login', 303)
        resp.delete_cookie(SESSION_COOKIE, path='/')
        return resp

    @app.get('/admin')
    @admin_required
    def index():
        rows = []
        for t in tenants.all_tenants(conn()):
            n = len(tenants.confirmed_recipients(conn(), t['id']))
            rows.append({'t': t, 'recipients': n})
        return render_template('admin/index.html', rows=rows)

    def tenant_or_404(tid):
        t = tenants.get(conn(), tid)
        if t is None:
            abort(404)
        return t

    @app.get('/admin/t/<tid>')
    @admin_required
    def tenant(tid):
        t = tenant_or_404(tid)
        return render_template('admin/tenant.html', t=t,
                               runs=tenants.runs(conn(), tid),
                               events=tenants.audit_log(conn(), tid),
                               recipients=tenants.recipients(conn(), tid),
                               children=tenants.children(t))

    @app.post('/admin/t/<tid>/<action>')
    @admin_required
    def tenant_action(tid, action):
        t = tenant_or_404(tid)
        if action == 'pause':
            tenants.update(conn(), tid, status='paused', pause_reason='admin')
            tenants.audit(conn(), tid, 'Sat på pause af administrator')
        elif action == 'resume':
            if t['password_enc'] and t['hostname']:
                tenants.update(conn(), tid, status='active',
                               pause_reason=None, fail_count=0)
                tenants.audit(conn(), tid, 'Genoptaget af administrator')
        elif action == 'run':
            tenants.update(conn(), tid, run_requested=1)
        elif action == 'delete':
            if request.form.get('confirm') != 'SLET':
                abort(400)
            lock = runner.tenant_lock(tid)
            if not lock.acquire(timeout=30):
                abort(409)
            try:
                tenants.delete(conn(), settings, tid)
            finally:
                lock.release()
            return redirect('/admin', 303)
        else:
            abort(404)
        return redirect('/admin/t/%s' % tid, 303)

    return app

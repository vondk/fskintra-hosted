"""Den offentlige app: tilmelding, login, indstillinger, status, sletning."""

import functools
import hmac
import json
import os
import re
import secrets

from flask import (Flask, Response, abort, g, make_response, redirect,
                   render_template, request)

from . import auth, jobs, mails, runner, security, tenants, ui
from .context import AiKeyCheckUnavailable
from .settings import AI_MODELS, DEFAULT_SECTIONS, SECTIONS

SESSION_COOKIE = '__Host-sid'
# Hemmeligheden der binder en engangskode til den browser/app, der bad om
# den (se auth.new_code)
CODE_COOKIE = '__Host-code'
CODE_PURPOSES = ('signup_code', 'login_code')
# Valgt farvetema. Uden cookien følger siden systemets lyse/mørke tilstand
THEME_COOKIE = '__Host-tema'
THEMES = ('light', 'dark')
# Kun stier på samme site - ellers kunne /tema bruges til at sende folk
# videre til et fremmed site
NEXT_RE = re.compile(r'/(?![/\\])[A-Za-z0-9/_-]*')
# Hvor man lander efter et temaskift på en side, der er svar på en POST
THEME_NEXT_AFTER_POST = {'/settings': '/settings', '/login': '/kode',
                         '/signup': '/kode', '/kode': '/kode'}
USERNAME_RE = re.compile(r'^[-.a-zA-Z0-9]{1,100}$')
AI_KEY_RE = re.compile(r'^sk-ant-[A-Za-z0-9_-]{20,300}$')

MESSAGES = {
    'saved': 'Indstillingerne er gemt.',
    'login_ok': 'Login virker, og indstillingerne er gemt.',
    'paused': 'Hentningen er sat på pause.',
    'resumed': 'Hentningen kører igen.',
    'queued': 'Hentningen starter inden for et minut.',
    'recipient_pending': 'Vi har sendt et link til din login-mail, som du '
                         'skal klikke på for at godkende den nye modtager.',
    'recipient_removed': 'Modtageren er fjernet.',
    'logged_out_all': 'Du er logget ud på alle andre enheder.',
}

PAUSE_REASONS = {
    'user': 'Du har selv sat hentningen på pause.',
    'login': 'ForældreIntra afviste dit login. Opdatér brugernavn/kodeord '
             'under Indstillinger.',
    'admin': 'Administratoren har sat hentningen på pause.',
}


def create_public_app(ctx):
    base = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, template_folder=os.path.join(base, 'templates'),
                static_folder=os.path.join(base, 'static'))
    app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
    security.install(app, csrf_cookie='__Host-csrf', secure=True,
                     expected_url=ctx.settings.public_url)
    ui.install(app)
    settings = ctx.settings

    def conn():
        return ctx.db()

    def client_ip():
        if settings.trust_fly_client_ip:
            ip = request.headers.get('Fly-Client-IP')
            if ip:
                return ip
        return request.remote_addr or '?'

    def limited(kind, value, limit, window=3600):
        return auth.rate_limited(conn(), kind, value, limit, window)

    public_host = settings.public_url.split('://', 1)[1].split('/', 1)[0]

    @app.before_request
    def _canonical_host():
        # Altid samme adresse (fx ikke *.fly.dev ved eget domæne), så
        # cookies og Origin-tjek passer. Fly's health check rammer /healthz
        if request.host != public_host and request.path != '/healthz':
            return redirect(settings.public_url + request.full_path.rstrip(
                '?'), 301)

    @app.before_request
    def _load_session():
        g.tenant = None
        g.session_token = request.cookies.get(SESSION_COOKIE, '')
        row = auth.load_session(conn(), g.session_token)
        if row is not None:
            g.tenant = tenants.get(conn(), row['tenant_id'])

    def theme_next():
        if request.method == 'GET':
            return request.path
        return THEME_NEXT_AFTER_POST.get(request.path, '/')

    @app.context_processor
    def _globals():
        theme = request.cookies.get(THEME_COOKIE)
        return {'tenant': g.get('tenant'), 'public_url': settings.public_url,
                'theme': theme if theme in THEMES else None,
                'theme_next': theme_next,
                'operator_name': settings.operator_name,
                'contact_email': settings.contact_email,
                'retention_days': settings.cache_retention_days}

    def login_required(view):
        @functools.wraps(view)
        def wrapper(*a, **kw):
            if g.tenant is None:
                return redirect('/', 302)
            return view(*a, **kw)
        return wrapper

    def start_session(tenant_id):
        token = auth.create_session(conn(), tenant_id)
        resp = redirect('/settings' if tenants.get(conn(), tenant_id)
                        ['status'] == 'new' else '/status', 303)
        resp.set_cookie(SESSION_COOKIE, token, secure=True, httponly=True,
                        samesite='Lax', path='/',
                        max_age=auth.SESSION_ABSOLUTE)
        return resp

    def send_link(email, purpose, tenant_id=None, payload=None):
        token = auth.new_token(conn(), purpose, email, tenant_id, payload)
        return '%s/auth/%s' % (settings.public_url, token)

    def send_login(email, purpose, tenant_id=None):
        '''Login- eller tilmeldingsmail med både link og engangskode.
        Returnerer hemmeligheden til CODE_COOKIE'''
        url = send_link(email, purpose, tenant_id)
        secret, code = auth.new_code(conn(), purpose + '_code', email,
                                     tenant_id)
        build = mails.login if purpose == 'login' else mails.signup
        ctx.send_mail(email, build(url, code))
        return secret

    def check_email(secret=None):
        '''"Tjek din mail" med feltet til koden. Cookien sættes altid, også
        når adressen ikke findes, så svaret ikke afslører noget'''
        resp = make_response(render_template('check_email.html',
                                             has_code=True))
        resp.set_cookie(CODE_COOKIE, secret or secrets.token_urlsafe(32),
                        secure=True, httponly=True, samesite='Lax',
                        path='/', max_age=auth.TOKEN_TTL['login_code'])
        return resp

    def message(title, text, status=200):
        return render_template('message.html', title=title, text=text), status

    # Offentlige sider

    @app.get('/healthz')
    def healthz():
        conn().execute('SELECT 1')
        return Response('ok', mimetype='text/plain')

    # Browsere og iOS beder om ikonerne her, selv om siderne linker til dem
    @app.get('/favicon.ico')
    @app.get('/apple-touch-icon.png')
    @app.get('/apple-touch-icon-precomposed.png')
    def icon():
        return app.send_static_file('apple-touch-icon.png')

    @app.get('/')
    def index():
        if g.tenant is not None:
            return redirect('/status', 302)
        return render_template('index.html',
                               deleted=request.args.get('slettet') == '1',
                               has_code=bool(request.cookies.get(
                                   CODE_COOKIE)))

    @app.get('/manifest.webmanifest')
    def manifest():
        '''Gør siden til en web app, der kan lægges på hjemmeskærmen'''
        asset = app.jinja_env.globals['asset']
        data = {
            'id': '/',
            'name': 'ForældreIntra til mail',
            'short_name': 'Intra-mail',
            'description': 'Nyt fra ForældreIntra direkte i din mail.',
            'lang': 'da',
            'dir': 'ltr',
            'start_url': '/',
            'scope': '/',
            'display': 'standalone',
            'background_color': '#f8f7f3',
            'theme_color': '#ffffff',
            'icons': [
                {'src': asset('icon-192.png'), 'sizes': '192x192',
                 'type': 'image/png', 'purpose': 'any'},
                {'src': asset('icon-512.png'), 'sizes': '512x512',
                 'type': 'image/png', 'purpose': 'any'},
                {'src': asset('icon-maskable-512.png'), 'sizes': '512x512',
                 'type': 'image/png', 'purpose': 'maskable'},
            ],
        }
        return Response(json.dumps(data, ensure_ascii=False),
                        mimetype='application/manifest+json')

    @app.post('/tema')
    def set_theme():
        theme = request.form.get('theme')
        nxt = request.form.get('next', '/')
        resp = redirect(nxt if NEXT_RE.fullmatch(nxt) else '/', 303)
        if theme in THEMES:
            resp.set_cookie(THEME_COOKIE, theme, secure=True, httponly=True,
                            samesite='Lax', path='/', max_age=400 * 86400)
        else:
            resp.delete_cookie(THEME_COOKIE, path='/', secure=True,
                               httponly=True, samesite='Lax')
        return resp

    @app.get('/privatliv')
    def privacy():
        return render_template('privatliv.html')

    @app.post('/signup')
    def signup():
        email = tenants.normalize_email(request.form.get('email'))
        invite = request.form.get('invite', '')
        if email is None:
            return render_template('index.html', signup_error='Angiv en '
                                   'gyldig mailadresse.'), 400
        if limited('signup-ip', client_ip(), 10) or \
                limited('signup-email', email, 3):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        if not hmac.compare_digest(invite.encode('utf-8'),
                                   settings.invite_code.encode('utf-8')):
            return render_template('index.html', signup_error='Forkert '
                                   'invitationskode.'), 400
        existing = tenants.get_by_email(conn(), email)
        if existing is not None:
            secret = send_login(email, 'login', existing['id'])
        else:
            secret = send_login(email, 'signup')
        return check_email(secret)

    @app.post('/login')
    def login():
        email = tenants.normalize_email(request.form.get('email'))
        if email is None:
            return render_template('index.html', login_error='Angiv en '
                                   'gyldig mailadresse.'), 400
        if limited('login-ip', client_ip(), 20) or \
                limited('login-email', email, 5):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        tenant = tenants.get_by_email(conn(), email)
        secret = None
        if tenant is not None:
            secret = send_login(email, 'login', tenant['id'])
        # Samme svar uanset om adressen findes (ingen enumeration)
        return check_email(secret)

    # En web app på hjemmeskærmen kan genåbne den seneste adresse med GET
    @app.get('/login')
    @app.get('/signup')
    def login_get():
        return redirect('/kode' if request.cookies.get(CODE_COOKIE)
                        else '/', 303)

    @app.get('/kode')
    def code_page():
        if g.tenant is not None:
            return redirect('/status', 302)
        return render_template('check_email.html', has_code=bool(
            request.cookies.get(CODE_COOKIE)))

    @app.post('/kode')
    def code_use():
        if limited('code-ip', client_ip(), 30):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        secret = request.cookies.get(CODE_COOKIE, '')
        code = re.sub(r'[^0-9]', '', request.form.get('code', ''))[:6]
        row = auth.peek_token(conn(), secret, CODE_PURPOSES)
        # Højst 10 forsøg i døgnet pr. adresse, uanset hvor mange koder der
        # bestilles. Samme svar som en forkert kode (ingen enumeration)
        if row is not None and not limited('code-email', row['email'], 10,
                                           86400):
            row = auth.consume_code(conn(), secret, code, CODE_PURPOSES)
        else:
            row = None
        if row is None:
            return render_template(
                'check_email.html', has_code=bool(secret),
                code_error='Koden er forkert eller udløbet. Prøv igen, bed '
                           'om en ny, eller brug linket i mailen.'), 400
        resp = login_with(row)
        resp.delete_cookie(CODE_COOKIE, path='/', secure=True,
                           httponly=True, samesite='Lax')
        return resp

    def login_with(row):
        '''Session ud fra et brugt signup-/login-link eller en kode'''
        purpose, email = row['purpose'], row['email']
        if purpose in ('signup', 'signup_code'):
            tenant = tenants.get_by_email(conn(), email)
            tid = tenant['id'] if tenant else tenants.create(conn(), email)
            tenants.audit(conn(), tid, 'Logget ind')
            return start_session(tid)
        tenant = tenants.get(conn(), row['tenant_id'])
        if tenant is None or tenant['email'] != email:
            abort(404)
        tenants.audit(conn(), tenant['id'], 'Logget ind')
        return start_session(tenant['id'])

    LINK_PURPOSES = ('signup', 'login', 'approve_recipient',
                     'confirm_recipient')

    @app.get('/auth/<token>')
    def auth_confirm(token):
        if limited('auth-ip', client_ip(), 60):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        row = auth.peek_token(conn(), token, LINK_PURPOSES)
        if row is None:
            return message('Linket virker ikke', 'Linket er udløbet eller '
                           'allerede brugt. Bed om et nyt fra forsiden.', 404)
        return render_template('auth_confirm.html', purpose=row['purpose'],
                               email=row['email'])

    @app.post('/auth/<token>')
    def auth_use(token):
        if limited('auth-ip', client_ip(), 60):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        row = auth.consume_token(conn(), token, LINK_PURPOSES)
        if row is None:
            return message('Linket virker ikke', 'Linket er udløbet eller '
                           'allerede brugt. Bed om et nyt fra forsiden.', 404)
        purpose = row['purpose']
        if purpose in ('signup', 'login'):
            return login_with(row)

        tid = row['tenant_id']
        rcpt = tenants.get_recipient(conn(), tid, int(row['payload'] or 0))
        if rcpt is None:
            abort(404)
        if purpose == 'approve_recipient':
            tenants.set_recipient(conn(), tid, rcpt['id'], 'approved_at')
            tenants.audit(conn(), tid, 'Modtager godkendt: %s' % rcpt['email'])
            url = send_link(rcpt['email'], 'confirm_recipient', tid,
                            str(rcpt['id']))
            ctx.send_mail(rcpt['email'], mails.confirm_recipient(
                url, tenants.get(conn(), tid)['email']))
            return message('Modtager godkendt', 'Vi har sendt en mail til %s,'
                           ' som skal bekræfte adressen.' % rcpt['email'])
        # confirm_recipient
        if rcpt['approved_at'] is None:
            abort(404)
        tenants.set_recipient(conn(), tid, rcpt['id'], 'confirmed_at')
        tenants.audit(conn(), tid, 'Modtager bekræftet: %s' % rcpt['email'])
        return message('Tak', '%s modtager nu mails fra ForældreIntra.'
                       % rcpt['email'])

    @app.post('/logout')
    def logout():
        auth.delete_session(conn(), g.session_token)
        resp = redirect('/', 303)
        resp.delete_cookie(SESSION_COOKIE, path='/', secure=True,
                           httponly=True, samesite='Lax')
        return resp

    # Sider der kræver login

    @app.post('/logout-all')
    @login_required
    def logout_all():
        auth.delete_all_sessions(conn(), g.tenant['id'])
        tenants.audit(conn(), g.tenant['id'], 'Logget ud overalt')
        return start_session(g.tenant['id'])

    @app.get('/status')
    @login_required
    def status():
        t = g.tenant
        return render_template(
            'status.html', msg=MESSAGES.get(request.args.get('m', '')),
            children=tenants.children(t),
            pause_reason=PAUSE_REASONS.get(t['pause_reason'] or ''),
            recipients=tenants.recipients(conn(), t['id']),
            runs=tenants.runs(conn(), t['id']),
            events=tenants.audit_log(conn(), t['id']),
            max_recipients=tenants.MAX_RECIPIENTS,
            run_hours=settings.run_hours,
            run_interval=settings.run_interval_minutes)

    def render_settings(error=None, status=200, form=None):
        t = g.tenant
        return render_template(
            'settings.html', error=error,
            msg=MESSAGES.get(request.args.get('m', '')),
            hosts=settings.allowed_school_hosts, sections=SECTIONS,
            selected=set(tenants.sections(t) or DEFAULT_SECTIONS),
            ai_models=AI_MODELS, shared_ai=bool(settings.shared_ai_key),
            has_password=bool(t['password_enc']),
            has_ai_key=bool(t['ai_key_enc']), form=form or {}), status

    @app.get('/settings')
    @login_required
    def settings_page():
        return render_settings()

    @app.post('/settings')
    @login_required
    def settings_save():
        t = g.tenant
        tid = t['id']
        f = request.form
        logintype = f.get('logintype', 'alm')
        hostname = f.get('hostname', '')
        username = f.get('username', '').strip()
        password = f.get('password', '')
        chosen = [s for s, _ in SECTIONS if f.get('section_' + s) == 'on']
        ai_mode = f.get('ai_mode', 'off')
        ai_key = f.get('ai_key', '').strip()
        ai_model = f.get('ai_model', AI_MODELS[0][0])
        form = {'logintype': logintype, 'hostname': hostname,
                'username': username, 'ai_mode': ai_mode,
                'ai_model': ai_model}

        if logintype not in ('alm', 'uni') or \
                hostname not in settings.allowed_school_hosts:
            return render_settings('Vælg skole og logintype.', 400, form)
        if not USERNAME_RE.match(username):
            return render_settings('Brugernavnet må kun indeholde a-z, 0-9, '
                                   'punktum og bindestreg.', 400, form)
        if len(password) > 200 or '\n' in password or '\r' in password:
            return render_settings('Ugyldigt kodeord.', 400, form)
        if not password and not t['password_enc']:
            return render_settings('Angiv dit ForældreIntra-kodeord.', 400,
                                   form)
        if not chosen:
            return render_settings('Vælg mindst ét afsnit.', 400, form)
        if ai_mode not in ('shared', 'own', 'off') or \
                ai_model not in dict(AI_MODELS):
            return render_settings('Ugyldigt AI-valg.', 400, form)
        if ai_mode == 'shared' and not settings.shared_ai_key:
            ai_mode = 'off'
        if ai_mode != 'off' and f.get('ai_consent') != 'on':
            return render_settings('Sæt flueben ved samtykke, hvis du vil '
                                   'bruge AI-resuméer - ellers vælg "Ingen '
                                   'AI".', 400, form)

        updates = {'sections': ','.join(chosen), 'ai_mode': ai_mode,
                   'ai_model': ai_model if ai_mode == 'own' else None}
        if ai_mode == 'own':
            if ai_key:
                if not AI_KEY_RE.match(ai_key):
                    return render_settings('API-nøglen har ikke det '
                                           'forventede format.', 400, form)
                if limited('ai-key-check', tid, 10):
                    return render_settings('For mange forsøg - prøv igen om '
                                           'en time.', 429, form)
                try:
                    ok = ctx.validate_ai_key(ai_key)
                except AiKeyCheckUnavailable:
                    return render_settings('Kunne ikke tjekke API-nøglen lige'
                                           ' nu. Prøv igen senere.', 503, form)
                if not ok:
                    return render_settings('Anthropic afviste API-nøglen.',
                                           400, form)
                updates['ai_key_enc'] = ctx.crypto.encrypt(ai_key, tid,
                                                           'ai_key')
            elif not t['ai_key_enc']:
                return render_settings('Angiv din API-nøgle.', 400, form)
        else:
            updates['ai_key_enc'] = None

        creds_changed = (bool(password) or logintype != t['logintype'] or
                         hostname != t['hostname'] or
                         username != t['username'])
        if creds_changed:
            # Test login før noget gemmes. Begrænset, så en forkert kode
            # ikke kan prøves mange gange og låse kontoen hos skolen
            if limited('check-login', tid, 5):
                return render_settings('For mange loginforsøg - prøv igen om'
                                       ' en time.', 429, form)
            profile = jobs.profile_for(
                ctx, t, password=password or None,
                overrides={'logintype': logintype, 'hostname': hostname,
                           'username': username})
            result = runner.run(settings, tid, profile, 'check',
                                lock_timeout=30)
            if result is None:
                return render_settings('En hentning kører lige nu - prøv '
                                       'igen om et minut.', 409, form)
            tenants.record_run(conn(), tid, result)
            if result.exit_code == runner.EXIT_LOGIN_FAILED:
                return render_settings('ForældreIntra afviste brugernavn, '
                                       'kodeord eller logintype. Intet er '
                                       'gemt.', 400, form)
            if result.exit_code != runner.EXIT_OK:
                return render_settings('Kunne ikke nå ForældreIntra lige nu. '
                                       'Intet er gemt - prøv igen senere.',
                                       502, form)
            account_changed = (hostname != t['hostname'] or
                               username != t['username'])
            updates.update(logintype=logintype, hostname=hostname,
                           username=username,
                           children=json.dumps(result.children(),
                                               ensure_ascii=False),
                           fail_count=0)
            if password:
                updates['password_enc'] = ctx.crypto.encrypt(password, tid,
                                                             'password')
            if account_changed:
                updates['needs_catchup'] = 1
            if t['status'] == 'new' or t['pause_reason'] == 'login':
                updates.update(status='active', pause_reason=None)
            tenants.audit(conn(), tid, 'Login til ForældreIntra opdateret')

        tenants.update(conn(), tid, **updates)
        if ai_mode != (t['ai_mode'] or 'off'):
            tenants.audit(conn(), tid, {
                'shared': 'AI-resuméer slået til (fælles nøgle, samtykke '
                          'givet)',
                'own': 'AI-resuméer slået til (egen nøgle, samtykke givet)',
                'off': 'AI-resuméer slået fra'}[ai_mode])
        return redirect('/status?m=%s' % ('login_ok' if creds_changed
                                          else 'saved'), 303)

    @app.post('/recipients/add')
    @login_required
    def recipient_add():
        tid = g.tenant['id']
        email = tenants.normalize_email(request.form.get('email'))
        if email is None:
            return message('Ugyldig adresse', 'Angiv en gyldig mailadresse.',
                           400)
        if limited('recipient-add', tid, 5):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        rid = tenants.add_recipient(conn(), tid, email)
        if rid is None:
            return message('Kunne ikke tilføjes', 'Adressen findes allerede, '
                           'eller du har nået grænsen på %d modtagere.'
                           % tenants.MAX_RECIPIENTS, 400)
        tenants.audit(conn(), tid, 'Modtager tilføjet (afventer '
                                   'godkendelse): %s' % email)
        # Godkendelse via login-mailen: en overtaget session kan ikke
        # omdirigere børnenes data til en fremmed adresse
        url = send_link(g.tenant['email'], 'approve_recipient', tid, str(rid))
        ctx.send_mail(g.tenant['email'], mails.approve_recipient(url, email))
        return redirect('/status?m=recipient_pending', 303)

    @app.post('/recipients/remove')
    @login_required
    def recipient_remove():
        tid = g.tenant['id']
        try:
            rid = int(request.form.get('id', ''))
        except ValueError:
            abort(400)
        rcpt = tenants.get_recipient(conn(), tid, rid)
        if rcpt is None or not tenants.remove_recipient(
                conn(), tid, rid, g.tenant['email']):
            abort(404)
        tenants.audit(conn(), tid, 'Modtager fjernet: %s' % rcpt['email'])
        return redirect('/status?m=recipient_removed', 303)

    @app.post('/pause')
    @login_required
    def pause():
        t = g.tenant
        if t['status'] == 'active':
            tenants.update(conn(), t['id'], status='paused',
                           pause_reason='user')
            tenants.audit(conn(), t['id'], 'Sat på pause')
        return redirect('/status?m=paused', 303)

    @app.post('/resume')
    @login_required
    def resume():
        t = g.tenant
        if t['status'] == 'paused' and t['pause_reason'] == 'user':
            tenants.update(conn(), t['id'], status='active',
                           pause_reason=None)
            tenants.audit(conn(), t['id'], 'Genoptaget')
        return redirect('/status?m=resumed', 303)

    @app.post('/run-now')
    @login_required
    def run_now():
        t = g.tenant
        if t['status'] != 'active':
            return redirect('/status', 303)
        if limited('run-now', t['id'], 3):
            return message('For mange forsøg', 'Du kan højst starte en '
                           'hentning tre gange i timen.', 429)
        tenants.update(conn(), t['id'], run_requested=1)
        return redirect('/status?m=queued', 303)

    @app.get('/export')
    @login_required
    def export():
        data = tenants.export(conn(), g.tenant['id'])
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition':
                     'attachment; filename="fskintra-mine-data.json"'})

    @app.post('/delete')
    @login_required
    def delete():
        # iPhones skriver gerne "Slet" med stort begyndelsesbogstav
        if (request.form.get('confirm') or '').strip().upper() != 'SLET':
            return message('Ikke slettet', 'Skriv SLET i feltet for at '
                           'bekræfte.', 400)
        tid = g.tenant['id']
        lock = runner.tenant_lock(tid)
        if not lock.acquire(timeout=30):
            return message('Prøv igen om lidt', 'En hentning kører lige nu. '
                           'Prøv igen om et par minutter.', 409)
        try:
            tenants.delete(conn(), settings, tid)
        finally:
            lock.release()
        resp = redirect('/?slettet=1', 303)
        resp.delete_cookie(SESSION_COOKIE, path='/', secure=True,
                           httponly=True, samesite='Lax')
        return resp

    @app.errorhandler(400)
    def _400(e):
        return message('Ugyldig forespørgsel', 'Siden var forældet eller '
                       'formularen ugyldig. Gå tilbage, genindlæs og prøv '
                       'igen.', 400)

    @app.errorhandler(404)
    def _404(e):
        return message('Ikke fundet', 'Siden findes ikke.', 404)

    @app.errorhandler(500)
    def _500(e):
        return message('Der skete en fejl', 'Prøv igen om lidt.', 500)

    @app.errorhandler(405)
    def _405(e):
        return message('Siden findes ikke sådan', 'Gå til forsiden og prøv '
                       'igen.', 405)

    @app.errorhandler(413)
    def _413(e):
        return message('For stor forespørgsel', 'Formularen er for stor.',
                       413)

    return app

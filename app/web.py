"""Den offentlige app: tilmelding, login, overblik, indstillinger, status,
sletning."""

import datetime
import functools
import hmac
import json
import os
import re

from flask import (Flask, Response, abort, g, make_response, redirect,
                   render_template, request)

from . import (auth, ics, jobs, mails, overview, runner, security,
               tenants, ui)
from .context import AiKeyCheckUnavailable
from .settings import AI_MODELS, DEFAULT_SECTIONS, SECTIONS

SESSION_COOKIE = '__Host-sid'
# Tilfældig værdi der binder en engangskode til den browser/app, der bad om
# den (se auth.new_code)
CODE_COOKIE = '__Host-code'
CODE_PURPOSES = ('signup_code', 'login_code')
# Valgt farvetema. Uden cookien følger siden systemets lyse/mørke tilstand
THEME_COOKIE = '__Host-tema'
THEMES = ('light', 'dark')
# Sider man kommer tilbage til efter et temaskift. Målet slås op her, så
# /tema aldrig sender videre til en adresse fra formularen (open redirect).
# Alle andre sider giver forsiden
THEME_RETURN = {p: p for p in ('/', '/overblik', '/status', '/settings',
                               '/privatliv', '/kode')}
# Efter en side, der er svar på en POST, lander man på en GET-side
THEME_RETURN_AFTER_POST = {'/settings': '/settings', '/login': '/kode',
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
    'ics_created': 'Kalenderlinket er klar.',
    'ics_new': 'Du har fået et nyt kalenderlink. Det gamle virker ikke '
               'længere.',
    'ics_removed': 'Kalenderlinket er slået fra.',
}
# Vises ved kalenderafsnittet og ikke øverst på siden
CALENDAR_MESSAGES = ('ics_created', 'ics_new', 'ics_removed')

# En kalender henter typisk feedet et par gange i timen. Grænsen gælder
# alle, der har linket, tilsammen
ICS_FETCHES_PER_HOUR = 120

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
    ui.install(app, ctx.settings.version)
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
            return THEME_RETURN.get(request.path, '/')
        return THEME_RETURN_AFTER_POST.get(request.path, '/')

    @app.context_processor
    def _globals():
        theme = request.cookies.get(THEME_COOKIE)
        return {'tenant': g.get('tenant'), 'public_url': settings.public_url,
                'theme': theme if theme in THEMES else None,
                'theme_next': theme_next,
                'operator_name': settings.operator_name,
                'retention_days': settings.cache_retention_days,
                'run_schedule': run_schedule(settings)}

    def login_required(view):
        @functools.wraps(view)
        def wrapper(*a, **kw):
            if g.tenant is None:
                return redirect('/', 302)
            return view(*a, **kw)
        return wrapper

    def start_session(tenant_id, target='/overblik'):
        token = auth.create_session(conn(), tenant_id)
        resp = redirect('/settings' if tenants.get(conn(), tenant_id)
                        ['status'] == 'new' else target, 303)
        resp.set_cookie(SESSION_COOKIE, token, secure=True, httponly=True,
                        samesite='Lax', path='/',
                        max_age=auth.SESSION_ABSOLUTE)
        return resp

    def send_link(email, purpose, tenant_id=None, payload=None):
        token = auth.new_token(conn(), purpose, email, tenant_id, payload)
        return '%s/auth/%s' % (settings.public_url, token)

    def send_login(email, purpose, tenant_id=None):
        '''Login- eller tilmeldingsmail med både link og engangskode.
        Returnerer værdien til CODE_COOKIE'''
        url = send_link(email, purpose, tenant_id)
        nonce, code = auth.new_code(conn(), purpose + '_code', email,
                                    tenant_id)
        build = mails.login if purpose == 'login' else mails.signup
        ctx.send_mail_soon(email, build(url, code))
        return nonce

    def check_email(nonce=None):
        '''"Tjek din mail" med feltet til koden. Cookien sættes altid, også
        når adressen ikke findes, så svaret ikke afslører noget'''
        resp = make_response(render_template('check_email.html',
                                             has_code=True))
        resp.set_cookie(CODE_COOKIE, nonce or auth.new_nonce(),
                        secure=True, httponly=True, samesite='Lax',
                        path='/', max_age=auth.TOKEN_TTL['login_code'])
        return resp

    def message(title, text, status=200):
        return render_template('message.html', title=title, text=text), status

    def plain(text, status):
        resp = Response(text + '\n', status, mimetype='text/plain')
        if status == 429:
            resp.headers['Retry-After'] = '3600'
        return resp

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
            return redirect('/overblik', 302)
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
        resp = redirect(THEME_RETURN.get(request.form.get('next', ''), '/'),
                        303)
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
            nonce = send_login(email, 'login', existing['id'])
        else:
            nonce = send_login(email, 'signup')
        return check_email(nonce)

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
        nonce = None
        if tenant is not None:
            nonce = send_login(email, 'login', tenant['id'])
        # Samme svar uanset om adressen findes (ingen enumeration)
        return check_email(nonce)

    # En web app på hjemmeskærmen kan genåbne den seneste adresse med GET
    @app.get('/login')
    @app.get('/signup')
    def login_get():
        return redirect('/kode' if request.cookies.get(CODE_COOKIE)
                        else '/', 303)

    @app.get('/kode')
    def code_page():
        if g.tenant is not None:
            return redirect('/overblik', 302)
        return render_template('check_email.html', has_code=bool(
            request.cookies.get(CODE_COOKIE)))

    @app.post('/kode')
    def code_use():
        if g.tenant is not None:
            # Dobbelttryk, hvor første svar nåede at logge ind: samme sted
            # hen som første svar
            return redirect('/settings' if g.tenant['status'] == 'new'
                            else '/overblik', 303)
        if limited('code-ip', client_ip(), 30):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        nonce = request.cookies.get(CODE_COOKIE, '')
        code = re.sub(r'[^0-9]', '', request.form.get('code', ''))[:6]
        row = auth.peek_token(conn(), nonce, CODE_PURPOSES)
        # Højst 10 forsøg i døgnet pr. adresse, uanset hvor mange koder der
        # bestilles. Samme svar som en forkert kode (ingen enumeration)
        if row is not None and not limited('code-email', row['email'], 10,
                                           86400):
            row = auth.consume_code(conn(), nonce, code, CODE_PURPOSES)
        else:
            row = None
        if row is None:
            # Dobbelttryk: samme kode fra samme browser lige efter brug
            row = auth.repeat_code(conn(), nonce, code, CODE_PURPOSES)
        if row is None:
            return render_template(
                'check_email.html', has_code=bool(nonce),
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
            ctx.send_mail_soon(rcpt['email'], mails.confirm_recipient(
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
        return start_session(g.tenant['id'], '/status')

    @app.get('/overblik')
    @login_required
    def overview_page():
        t = g.tenant
        data, mtime = overview.read(settings, t['id'])
        view = None
        if data is not None:
            view = overview.view(data, datetime.date.today(), mtime)
        return render_template(
            'overblik.html', view=view,
            weekplans='pln' in (tenants.sections(t) or DEFAULT_SECTIONS),
            pause_reason=PAUSE_REASONS.get(t['pause_reason'] or ''))

    def calendar_links(t):
        '''Adresserne til forælderens kalenderlink - eller None, hvis der
        ikke er et link (eller det ikke kan dekrypteres)'''
        token = tenants.ics_token(ctx.crypto, t)
        if not token:
            return None
        path = '/kalender/%s.ics' % token
        # webcals og ikke webcal: det sidste betyder http, og så ville
        # tokenet blive sendt ukrypteret, før Fly sender videre til https
        return {'url': settings.public_url + path,
                'subscribe': 'webcals://%s%s' % (public_host, path)}

    @app.get('/status')
    @login_required
    def status():
        t = g.tenant
        m = request.args.get('m', '')
        text = MESSAGES.get(m)
        in_calendar = m in CALENDAR_MESSAGES
        return render_template(
            'status.html', msg=None if in_calendar else text,
            cal_msg=text if in_calendar else None, cal=calendar_links(t),
            children=tenants.children(t),
            pause_reason=PAUSE_REASONS.get(t['pause_reason'] or ''),
            recipients=tenants.recipients(conn(), t['id']),
            runs=tenants.runs(conn(), t['id']),
            events=tenants.audit_log(conn(), t['id']),
            max_recipients=tenants.MAX_RECIPIENTS)

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
        ctx.send_mail_soon(g.tenant['email'],
                           mails.approve_recipient(url, email))
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

    # Kalenderlink

    @app.post('/kalender/nyt')
    @login_required
    def calendar_new():
        t = g.tenant
        if limited('ics-new', t['id'], 10):
            return message('For mange forsøg', 'Prøv igen om en time.', 429)
        renewed = bool(t['ics_token_hash'])
        tenants.new_ics_token(conn(), ctx.crypto, t['id'])
        tenants.audit(conn(), t['id'], 'Kalenderlink fornyet' if renewed
                      else 'Kalenderlink oprettet')
        return redirect('/status?m=%s#kalender'
                        % ('ics_new' if renewed else 'ics_created'), 303)

    @app.post('/kalender/slet')
    @login_required
    def calendar_remove():
        t = g.tenant
        if not t['ics_token_hash']:
            return redirect('/status#kalender', 303)
        tenants.clear_ics_token(conn(), t['id'])
        tenants.audit(conn(), t['id'], 'Kalenderlink slået fra')
        return redirect('/status?m=ics_removed#kalender', 303)

    @app.get('/kalender/<token>.ics')
    def calendar_feed(token):
        '''Feedet til en kalender-app eller -tjeneste. Tokenet i adressen
        er den eneste adgangskontrol - der er hverken session eller CSRF'''
        # En kalender-app skal ikke have en cookie. Svarene er ren tekst, for
        # det er et program og ikke en browser, der læser dem
        g.csrf_new = False
        t = tenants.get_by_ics_token(conn(), token)
        if t is None:
            if limited('ics-miss', client_ip(), 60):
                return plain('For mange forsøg', 429)
            return plain('Kalenderlinket virker ikke', 404)
        # Pr. forælder og ikke pr. IP: Google og Apple henter for mange
        # brugere fra de samme adresser
        if limited('ics', t['id'], ICS_FETCHES_PER_HOUR):
            return plain('For mange forsøg', 429)
        data, mtime = overview.read(settings, t['id'])
        return Response(
            ics.build(data, datetime.date.today(), t['id'], public_host,
                      mtime),
            mimetype='text/calendar',
            headers={'Content-Disposition': 'inline; filename="skole.ics"'})

    @app.get('/export')
    @login_required
    def export():
        data = tenants.export(conn(), g.tenant['id'])
        # Det seneste overblik, præcis som fskintra skrev det
        data['overblik'] = overview.load(settings, g.tenant['id'])
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


def run_schedule(settings):
    '''Fx "hver halve time kl. 6-22" - til teksterne på siden'''
    n = settings.run_interval_minutes
    every = {30: 'hver halve time', 60: 'hver time'}.get(
        n, 'hvert %d. minut' % n)
    return '%s kl. %d-%d' % (every, settings.run_hours[0],
                             settings.run_hours[1])

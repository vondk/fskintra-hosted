"""Indhold af systemmails. Hver mail sendes som både tekst og HTML (samme
indhold), bygget af app/templates/mail/layout.{txt,html}. HTML-delen er
autoescaped, bruger kun inline styles og ingen eksterne billeder."""

import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

SERVICE = 'ForældreIntra til mail'
BLUE = '#1f6feb'
AMBER = '#b45309'

_env = Environment(
    loader=FileSystemLoader(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'templates', 'mail')),
    autoescape=select_autoescape(['html']),
    keep_trailing_newline=True)


def _once():
    return ('Linket virker i 15 minutter og kan kun bruges én gang.',
            'Har du ikke bedt om det, kan du blot slette mailen.')


def signup(url):
    return dict(
        subject='Bekræft din tilmelding',
        preheader='Ét klik, så er du i gang.',
        heading='Velkommen - bekræft din tilmelding',
        paragraphs=[
            'Tak fordi du vil prøve %s. Tjenesten henter nyt fra '
            'ForældreIntra for dig og sender det som almindelige mails.'
            % SERVICE,
            'Klik på knappen for at oprette din konto. Bagefter vælger du '
            'skole og indtaster dit login til ForældreIntra.'],
        button_label='Opret konto', button_url=url, notes=_once())


def login(url):
    return dict(
        subject='Dit login-link',
        preheader='Klik for at logge ind.',
        heading='Log ind',
        paragraphs=['Klik på knappen for at logge ind på %s.' % SERVICE],
        button_label='Log ind', button_url=url, notes=_once())


def approve_recipient(url, recipient):
    return dict(
        subject='Godkend ny modtager',
        preheader='Der er bedt om at sende mails til %s.' % recipient,
        heading='Godkend ny modtager',
        paragraphs=[
            'Der er bedt om, at mails fra ForældreIntra også skal sendes '
            'til %s.' % recipient,
            'Når du har godkendt, skal modtageren selv bekræfte sin '
            'adresse.'],
        button_label='Godkend modtager', button_url=url,
        notes=('Linket virker i 48 timer.',
               'Har du ikke selv bedt om det, så klik IKKE. Log i stedet '
               'ind og vælg "Log ud på alle andre enheder".'))


def confirm_recipient(url, owner):
    return dict(
        subject='Bekræft at du vil modtage mails',
        preheader='%s vil sende nyt fra ForældreIntra til dig.' % owner,
        heading='Vil du modtage mails fra ForældreIntra?',
        paragraphs=[
            '%s vil gerne sende nyt fra ForældreIntra (beskeder, '
            'ugeplaner, lektier og billeder) til denne adresse.' % owner],
        button_label='Ja, send mails til mig', button_url=url,
        notes=('Linket virker i 48 timer.',
               'Kender du ikke afsenderen, kan du blot slette mailen.'))


def login_failed(settings_url):
    return dict(
        subject='ForældreIntra afviste dit login',
        preheader='Hentningen er sat på pause - opdatér dit kodeord.',
        heading='ForældreIntra afviste dit login',
        paragraphs=[
            'Ved seneste hentning afviste ForældreIntra dit brugernavn '
            'eller kodeord. Vi har sat hentningen på pause, så din konto '
            'hos skolen ikke bliver låst af gentagne forsøg.',
            'Har du skiftet kodeord, så opdatér det under Indstillinger. '
            'Hentningen starter igen, så snart login virker.'],
        button_label='Opdatér kodeord', button_url=settings_url,
        accent=AMBER)


def admin_login(url):
    return dict(
        subject='Admin: log ind',
        preheader='Login-link til admin.',
        heading='Log ind som administrator',
        paragraphs=['Linket virker kun gennem `fly proxy 9090`.'],
        button_label='Log ind', button_url=url, notes=_once())


def admin_alert(tenant_short, hostname, failures, exit_code, url):
    return dict(
        subject='%d fejl i træk for en forælder' % failures,
        preheader='Tenant %s fejler.' % tenant_short,
        heading='%d fejl i træk' % failures,
        paragraphs=[
            'Hentningen for tenant %s (%s) har fejlet %d gange i træk. '
            'Seneste exit-kode: %s.'
            % (tenant_short, hostname or '-', failures, exit_code)],
        button_label='Se detaljer i admin', button_url=url, accent=AMBER)


def render(settings, mail):
    '''(subject, tekst, html) for en mail fra funktionerne ovenfor'''
    data = {
        'service': SERVICE,
        'operator': getattr(settings, 'operator_name', ''),
        'public_url': settings.public_url,
        'accent': BLUE,
        'notes': (),
        'button_url': '',
        'button_label': '',
        'preheader': '',
    }
    data.update(mail)
    text = _env.get_template('layout.txt').render(**data)
    html = _env.get_template('layout.html').render(**data)
    return data['subject'], text, html

import dataclasses
import smtplib

from app import mails
from app.mailer import FROM_NAME, Mailer
from conftest import INVITE, PUBLIC, post

URL = PUBLIC + '/auth/abc'


def test_render_has_text_and_html_with_link(ctx):
    subject, text, html = mails.render(ctx.settings, mails.signup(URL))
    assert subject == 'Bekræft din tilmelding'
    for part in (text, html):
        assert URL in part and 'Opret konto' in part
        assert '15 minutter' in part
    assert html.lstrip().startswith('<!doctype html>')
    assert '<script' not in html and '<img' not in html


def test_operator_name_in_footer_only_when_set(ctx):
    _s, text, html = mails.render(ctx.settings, mails.login(URL))
    assert 'Kim' not in text and 'Kim' not in html
    ctx.settings = dataclasses.replace(ctx.settings,
                                       operator_name='Kim von Müllen')
    _s, text, html = mails.render(ctx.settings, mails.login(URL))
    assert 'lavet af Kim von Müllen' in text
    assert 'lavet af Kim von Müllen' in html


def test_html_is_escaped(ctx):
    _s, _t, html = mails.render(ctx.settings, mails.confirm_recipient(
        URL, '<script>alert(1)</script>@example.net'))
    assert '<script>' not in html and '&lt;script&gt;' in html


def test_mailer_sends_multipart_alternative(ctx, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sent.append(msg)

        def quit(self):
            pass

    monkeypatch.setattr(smtplib, 'SMTP', FakeSMTP)
    subject, text, html = mails.render(ctx.settings, mails.login(URL))
    assert Mailer(ctx.settings.smtp).send('a@example.net', subject, text,
                                          html)
    (msg,) = sent
    assert msg.get_content_type() == 'multipart/alternative'
    types = [p.get_content_type() for p in msg.iter_parts()]
    assert types == ['text/plain', 'text/html']
    assert FROM_NAME in str(msg['From'])
    assert msg['Auto-Submitted'] == 'auto-generated'


def test_signup_sends_html_mail(client, ctx):
    post(client, '/signup', {'email': 'a@example.net', 'invite': INVITE})
    assert 'Opret konto' in ctx.mailer.html[-1]
    assert ctx.mailer.sent[-1][1] == 'Bekræft din tilmelding'


def test_footer_on_site_shows_operator(client, ctx):
    assert b'Lavet af' not in client.get('/').data
    ctx.settings = dataclasses.replace(ctx.settings,
                                       operator_name='Kim von Müllen')
    from app.web import create_public_app
    from conftest import make_client
    c = make_client(create_public_app(ctx))
    assert 'Lavet af Kim von Müllen'.encode() in c.get('/').data

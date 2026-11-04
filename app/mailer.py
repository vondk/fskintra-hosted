"""Systemmails (login-links, bekræftelser, statusbeskeder) som tekst og
HTML (indholdet bygges i app/mails.py).

Samme sikre SMTP-forbindelse som fskintra bruger: login sendes kun over
SSL (port 465) eller STARTTLS med tjek af certifikatet."""

import email.utils
import logging
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger(__name__)

FROM_NAME = 'ForældreIntra til mail'


class Mailer:
    def __init__(self, smtp):
        self.smtp = smtp

    def _context(self):
        ctx = ssl.create_default_context()
        if not self.smtp.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _connect(self):
        s = self.smtp
        if s.port == 465:
            server = smtplib.SMTP_SSL(s.host, s.port, context=self._context(),
                                      timeout=30)
        else:
            server = smtplib.SMTP(s.host, s.port, timeout=30)
        if s.user:
            if s.port != 465:
                try:
                    server.starttls(context=self._context())
                except Exception:
                    server.close()
                    raise
            server.login(s.user, s.password)
        return server

    def send(self, to, subject, body, html=None):
        '''Send en mail (tekst + evt. HTML-alternativ). Returnerer False (og
        logger) ved fejl - en systemmail må aldrig vælte en request eller en
        kørsel'''
        msg = EmailMessage()
        msg['From'] = email.utils.formataddr((FROM_NAME, self.smtp.sender))
        msg['To'] = to
        msg['Subject'] = subject
        msg['Date'] = email.utils.formatdate(localtime=True)
        domain = self.smtp.sender.rsplit('@', 1)[-1]
        msg['Message-ID'] = email.utils.make_msgid(domain=domain)
        msg['Auto-Submitted'] = 'auto-generated'
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype='html')
        try:
            server = self._connect()
            try:
                server.send_message(msg)
            finally:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass
        except (OSError, smtplib.SMTPException) as e:
            log.warning('Kunne ikke sende systemmail: %s', type(e).__name__)
            return False
        return True

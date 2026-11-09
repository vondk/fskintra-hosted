"""Fælles kontekst for web, admin og planlægger"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

from . import db, mails, tenants
from .crypto import Crypto
from .mailer import Mailer

log = logging.getLogger(__name__)


class AiKeyCheckUnavailable(Exception):
    '''Nøglen kunne ikke tjekkes lige nu (netværk/API nede)'''


def validate_anthropic_key(key):
    '''True hvis nøglen virker, False hvis den afvises'''
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=15.0)
    try:
        client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return False
    except (anthropic.APIConnectionError, anthropic.APIStatusError):
        raise AiKeyCheckUnavailable()
    return True


class Context:
    def __init__(self, settings, mailer=None, ai_key_validator=None,
                 mail_in_background=True):
        self.settings = settings
        # Én tråd: mails fra web sendes i rækkefølge og med højst én SMTP-
        # forbindelse ad gangen. Ventende mails sendes færdig ved nedlukning
        self._mail_pool = (ThreadPoolExecutor(max_workers=1,
                                              thread_name_prefix='mail')
                           if mail_in_background else None)
        self.crypto = Crypto(settings.master_key, settings.master_key_old)
        self.mailer = mailer or Mailer(settings.smtp)
        self.validate_ai_key = ai_key_validator or validate_anthropic_key
        os.makedirs(settings.tenants_dir, mode=0o700, exist_ok=True)
        conn = self.db()
        db.migrate(conn)
        rotated = tenants.rotate_keys(conn, self.crypto)
        if rotated:
            log.info('Genkrypterede %d værdier med den nye MASTER_KEY',
                     rotated)

    def db(self):
        return db.connect(self.settings.db_path)

    def send_mail(self, to, mail):
        '''Send en systemmail bygget af en funktion i app/mails.py'''
        subject, text, html = mails.render(self.settings, mail)
        return self.mailer.send(to, subject, text, html)

    def send_mail_soon(self, to, mail):
        '''Som send_mail, men SMTP-delen sker i baggrunden, så en request
        svarer med det samme i stedet for at vente sekunder på mailserveren.
        Det fjerner også tidsforskellen mellem en adresse, der findes, og en
        der ikke gør (ingen enumeration via svartid). Mailen bygges her, så
        en fejl i den stadig viser sig i requesten'''
        subject, text, html = mails.render(self.settings, mail)
        if self._mail_pool is None:
            self._send(to, subject, text, html)
        else:
            self._mail_pool.submit(self._send, to, subject, text, html)

    def _send(self, to, subject, text, html):
        try:
            self.mailer.send(to, subject, text, html)
        except Exception as e:  # Mailer.send fanger selv SMTP-fejl
            log.error('Systemmail fejlede: %s', type(e).__name__)

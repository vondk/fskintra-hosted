"""Fælles kontekst for web, admin og planlægger"""

import logging
import os

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
    def __init__(self, settings, mailer=None, ai_key_validator=None):
        self.settings = settings
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

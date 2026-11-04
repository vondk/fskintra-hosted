"""Indgang for gunicorn. Den offentlige app serveres af gunicorn; admin-appen
og planlæggeren startes i baggrunden i worker-processen (post_worker_init),
så der kun findes én af hver."""

import logging
import threading

from werkzeug.serving import make_server

from . import settings as settings_mod
from .admin import create_admin_app
from .context import Context
from .scheduler import Scheduler
from .web import create_public_app

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger(__name__)
# Werkzeugs request-log (admin-serveren) ville skrive URL'er med engangs-
# tokens (/admin/auth/<token>) i loggen
logging.getLogger('werkzeug').setLevel(logging.WARNING)

ctx = Context(settings_mod.load())
app = create_public_app(ctx)

_started = False


def start_background():
    global _started
    if _started:
        return
    _started = True

    # Planlæggeren startes først - en fejl i admin må ikke stoppe hentningen
    Scheduler(ctx).start()
    try:
        admin = make_server(ctx.settings.admin_bind, ctx.settings.admin_port,
                            create_admin_app(ctx), threaded=True)
    except OSError as e:
        log.error('Admin kunne ikke starte på [%s]:%d: %s',
                  ctx.settings.admin_bind, ctx.settings.admin_port, e)
        return
    threading.Thread(target=admin.serve_forever, name='admin',
                     daemon=True).start()
    log.info('Admin lytter på [%s]:%d', ctx.settings.admin_bind,
             ctx.settings.admin_port)

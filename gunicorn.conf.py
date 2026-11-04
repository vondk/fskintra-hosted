# gunicorn-opsætning. Én worker (planlægger og admin kører i den), flere
# tråde til web-requests. Ingen access-log: URL'er kan indeholde engangs-
# tokens (/auth/<token>), og de må ikke ende i logs.
bind = ['[::]:8080']
workers = 1
worker_class = 'gthread'
threads = 4
timeout = 120
graceful_timeout = 30
accesslog = None
errorlog = '-'
loglevel = 'info'
# Ingen kontrol-socket (styring af gunicorn udefra er ikke nødvendig)
control_socket_disable = True


def post_worker_init(worker):
    from app.wsgi import start_background
    start_background()

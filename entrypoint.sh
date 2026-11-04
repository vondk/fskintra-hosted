#!/bin/sh
# Starter som root KUN for at give brugeren 'app' ejerskab over volumet.
# Derefter droppes alle rettigheder (setpriv) for resten af levetiden.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/tenants"
find "$DATA_DIR" -xdev ! -user app -exec chown app:app {} +
chmod 0700 "$DATA_DIR" "$DATA_DIR/tenants"

exec setpriv --reuid=app --regid=app --init-groups --no-new-privs \
    --inh-caps=-all --bounding-set=-all \
    gunicorn -c /app/gunicorn.conf.py app.wsgi:app

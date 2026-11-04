# syntax=docker/dockerfile:1
# Base-image låst på digest - Dependabot foreslår opdateringer
FROM python:3.15-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd

ENV LC_ALL=da_DK.UTF-8 \
    LANG=da_DK.UTF-8 \
    TZ=Europe/Copenhagen \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# fskintra kræver en dansk locale (datoer) og tzdata. Brugeren 'app' ejer
# kun /data - koden ejes af root og kan ikke ændres af den kørende proces
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends locales tzdata && \
    sed -i '/da_DK.UTF-8/s/^# //' /etc/locale.gen && \
    locale-gen && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system app && \
    useradd --system --gid app --home-dir /data --no-create-home \
        --shell /usr/sbin/nologin app

WORKDIR /app

# Alle afhængigheder er låst med hashes (requirements.txt fra uv pip compile)
COPY requirements.txt .
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY app ./app
COPY vendor/fskintra/fskintra.py vendor/fskintra/LICENSE.txt ./vendor/fskintra/
COPY vendor/fskintra/skoleintra ./vendor/fskintra/skoleintra
COPY gunicorn.conf.py entrypoint.sh ./
RUN chmod 0755 entrypoint.sh && python -m compileall -q app vendor/fskintra

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]

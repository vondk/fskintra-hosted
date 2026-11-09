# fskintra-hosted

En lille tjeneste, der kører [fskintra](https://github.com/vondk/fskintra)
for flere forældre. Forældrene tilmelder sig på en webside med en
invitationskode og indtaster selv deres ForældreIntra-login. Derefter får de
nye beskeder, opslag, ugeplaner, lektier og billeder som mails, uden selv at
hoste noget og uden Home Assistant.

Kører på Fly.io i EU (én maskine og et krypteret volume, ca. 35 kr/md inkl.
moms for en klasse).

## Arkitektur

```
Fly Machine (arn), non-root
├─ gunicorn :8080  offentlig app (Flask, server-renderet, uden JavaScript,
│                  kan installeres som web app via manifest.webmanifest)
├─ :9090           admin, ikke eksponeret, kun via `fly proxy`
├─ planlægger      hver halve time kl. 6-22, én forælder ad gangen
│   └─ subprocess: vendor/fskintra/fskintra.py  (env kun fra en allowlist)
├─ /data/app.db         SQLite, hemmeligheder AES-256-GCM-krypteret
└─ /data/tenants/<id>/  FSKINTRA_ROOT pr. forælder (0700)
```

`vendor/fskintra` er et git submodule, der er låst til en bestemt commit.

## Sikkerhed i korte træk

* **Kodeord og API-nøgler** krypteres med AES-256-GCM. AAD binder dem til
  forælder og felt. Nøglen findes kun som Fly secret og sendes aldrig til
  fskintra-processen.
* **Login** sker kun med engangslinks eller -koder på mail. Der gemmes kun
  SHA-256 af tokens og sessions. Links skal bekræftes med et klik, så
  mail-scannere ikke kan bruge dem. Koden er til web appen på
  hjemmeskærmen (den har egne cookies) og virker kun sammen med en cookie
  i den browser, der bad om den.
* **Nye modtagere** skal godkendes fra login-mailen og bekræftes af
  modtageren selv.
* **Skoler** vælges fra en allowlist (`*.m.skoleintra.dk`). Det forhindrer
  SSRF.
* **Web:** CSRF, strict CSP uden JS, HSTS og rate limits. Der er ingen
  access-log, fordi URL'er kan indeholde tokens.
* **Afvist login** sætter forælderen på pause med det samme, så kontoen hos
  skolen ikke låses.
* **Dataminimering:** Der gemmes ingen kopier af sendte mails, og cachen
  slettes efter 30 dage.
* **Supply chain:** Afhængigheder er låst med hashes, og base-image og
  actions er låst på digest/SHA. Dependabot, CodeQL, bandit og pip-audit
  kører i CI.

Se [SECURITY.md](SECURITY.md) og [docs/drift.md](docs/drift.md).

## Udvikling

```bash
git clone --recurse-submodules https://github.com/vondk/fskintra-hosted
cd fskintra-hosted
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes --no-deps -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest
```

Testene kræver en dansk locale (`da_DK.UTF-8`). Afhængighederne opdateres
med:

```bash
uv pip compile --python-version 3.12 --generate-hashes requirements.in -o requirements.txt
```

## Licens

BSD 3-clause, se [LICENSE](LICENSE). `vendor/fskintra` har sin egen licens
(`vendor/fskintra/LICENSE.txt`).

# Drift på Fly.io

## 1. Forudsætninger

* Fly.io-konto **med 2FA** og `flyctl` installeret.
* Domæne til afsenderadressen og en SMTP-udbyder (fx Brevo, gratis op til
  300 mails/dag) med **SPF, DKIM og DMARC** sat op i DNS.
* Evt. en Anthropic API-nøgle i et separat workspace med lav *spend limit*.

## 2. Opret app og volume

```bash
fly apps create fskintra-hosted
fly volumes create data --app fskintra-hosted --region arn --size 1 --snapshot-retention 5
```

Volumet er krypteret som standard. Tjek med `fly volumes list`.

## 3. Hemmeligheder

Generér nøgler lokalt. De må aldrig ligge i git:

```bash
python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # MASTER_KEY
python3 -c "import secrets;print(secrets.token_urlsafe(16))"                      # INVITE_CODE
```

Gem `MASTER_KEY` i din password manager. Uden den skal alle forældre
indtaste deres kodeord igen.

```bash
fly secrets set --app fskintra-hosted \
  MASTER_KEY=... \
  INVITE_CODE=... \
  ADMIN_EMAIL=dig@example.dk \
  ALLOWED_SCHOOL_HOSTS=dinskole.m.skoleintra.dk \
  FSKINTRA_SENDER_EMAIL=skole@ditdomæne.dk \
  FSKINTRA_SMTP_HOST=smtp-relay.brevo.com \
  FSKINTRA_SMTP_USER=... \
  FSKINTRA_SMTP_PASSWORD=... \
  FSKINTRA_ANTHROPIC_API_KEY=...
```

Ret `PUBLIC_URL` i `fly.toml`, hvis du bruger eget domæne.

## 4. Deploy (præcis én maskine)

```bash
fly deploy --ha=false
fly scale count 1
```

Volumet hører til én maskine. To maskiner ville give to forskellige
databaser.

Eget domæne:

```bash
fly certs add skole.ditdomæne.dk
```

## 5. Første test (spike)

Tilmeld dig selv først. Når du gemmer indstillingerne, logger tjenesten ind
på ForældreIntra fra Fly. Vises "Login OK: fandt …", virker login fra
datacentret. Kør med en eller to andre forældre i en uge, før koden deles.

## 6. GitHub

* **Settings → Environments → `production`:** begræns til branch `main`, og
  tilføj secret `FLY_API_TOKEN` fra
  `fly tokens create deploy --app fskintra-hosted --expiry 8760h`.
* **Beskyt `main`:** *Settings → Rules → Rulesets → New ruleset → Import a
  ruleset*, og vælg `.github/rulesets/main.json`. Rulesettet blokerer
  force push og sletning og kræver en PR med løste tråde. Derudover skal
  `test`, `docker` og `analyze` være grønne, og der må ikke være CodeQL-fund
  på high eller derover. Der kræves 0 godkendelser, fordi du ikke kan
  godkende dine egne PR'er, og ingen kan omgå reglerne.
* **Code security:** slå secret scanning, push protection, Dependabot
  alerts og private vulnerability reporting til.

## 7. Admin

```bash
fly proxy 9090:9090 --app fskintra-hosted
```

Åbn derefter <http://localhost:9090/admin>. Brug `localhost`, ikke
`127.0.0.1`, fordi Origin-tjekket kræver præcis `ADMIN_URL`. Login-linket
sendes til `ADMIN_EMAIL`.

## 8. Drift

| Opgave | Hvordan |
|---|---|
| Logs | `fly logs`. Der logges kun tenant-id, exit-kode og varighed. |
| Rotér `MASTER_KEY` | Sæt den nye som `MASTER_KEY` og den gamle som `MASTER_KEY_OLD`. Ved næste start genkrypteres alt. Fjern derefter `MASTER_KEY_OLD`. |
| Skift invitationskode | `fly secrets set INVITE_CODE=...`. Eksisterende brugere påvirkes ikke. |
| Opdatér fskintra | Dependabot laver en PR for submodulet. Alternativt `git -C vendor/fskintra fetch && git -C vendor/fskintra checkout <tag>` og commit. |
| Backup/gendannelse | `fly volumes snapshots list <vol-id>`, derefter `fly volumes create data --snapshot-id <id>`. |
| Måling af hukommelse | `fly metrics` / Grafana. Peak under 200 MB gør det muligt at prøve 256 MB med swap. |

## 9. Verifikation efter deploy

```bash
curl -sI https://fskintra-hosted.fly.dev | grep -iE 'strict-transport|content-security|x-frame'
curl -s -m 5 http://fskintra-hosted.fly.dev:9090/ || echo "9090 er lukket udefra (godt)"
```

Kør desuden securityheaders.com og SSL Labs på domænet. Begge skal give A
eller bedre.

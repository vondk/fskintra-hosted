# Sikkerhed

## Rapportér en sårbarhed

Brug GitHubs **"Report a vulnerability"** (Security → Advisories) i dette
repo. Opret ikke et offentligt issue. Du får svar inden for et par dage.

## Trusselsmodel (kort)

| Trussel | Kontrol |
|---|---|
| Læk af database, volume eller snapshot | Kodeord og API-nøgler er krypteret med AES-256-GCM. Nøglen (`MASTER_KEY`) findes kun som Fly secret. Tokens og sessions gemmes kun som SHA-256. Kalenderlinket gemmes som SHA-256 (opslag) plus en krypteret kopi (så det kan vises igen). Volumet er krypteret at rest. |
| SSRF via skolens adresse | Kun adresser fra `ALLOWED_SCHOOL_HOSTS`, som desuden skal matche `*.m.skoleintra.dk` |
| Overtaget session omdirigerer mails | Nye modtagere skal godkendes fra login-mailen og bekræftes af modtageren. "Log ud overalt" findes. Login-mailen kan ikke ændres. |
| CSRF, XSS, clickjacking | Double-submit CSRF-token (primært værn) og Origin-tjek som ekstra lag (fremmed Origin afvises, `null` afgøres af tokenet). `Referrer-Policy: same-origin`. Jinja-autoescape. CSP `default-src 'none'` med `script-src 'self'`: ingen inline-scripts og ingen eval. Det eneste script er sidens egen kopiér-knap (`static/copy.js`), og alt virker uden JavaScript. `frame-ancestors 'none'`. |
| Brute force | Rate limits på tilmelding, login, links, login-tjek, "hent nu" og kalenderlinks. Invitationskoden skal være mindst 12 tegn. |
| Mail-scannere bruger engangslinks | GET viser kun en knap. Linket forbruges først ved POST. |
| Gæt eller tyveri af login-kode | Koden (6 cifre, 15 min.) virker kun sammen med en tilfældig `__Host-code`-cookie i den browser, der bad om den. Databasen har kun SHA-256 af cookien og af cookie+kode. Højst 5 forsøg pr. kode og 10 pr. adresse i døgnet. Samme svar uanset om adressen findes, også i svartid (mails sendes i baggrunden). Samme kode fra samme browser inden for 30 sek. efter brug accepteres (dobbelttryk uden JS) og tæller med i de 5 forsøg. |
| Læk eller gæt af kalenderlink | Linket (`/kalender/<token>.ics`, 256 bit) er valgfrit og giver kun læseadgang til kalenderen: lektier, afleveringer, ture og ændrede tider fra i dag og frem. Det giver ikke adgang til konto, indstillinger eller mails. Det kan fornyes eller slås fra på Status. Abonnementsknappen bruger `webcals://`, så tokenet ikke sendes over http. Der er ingen access-log, og fejl logges med rutens mønster i stedet for adressen. Svaret sætter ingen cookies og har `no-store`. Højst 120 hentninger i timen pr. link og 60 ukendte links i timen pr. IP. |
| Én forælder påvirker en anden | Én subprocess pr. kørsel med miljø fra en allowlist, eget katalog (0700), rlimits og timeout på hele procesgruppen. SQL er altid afgrænset til sessionens forælder. |
| Låst konto hos skolen | Exit-kode 3 (afvist login) giver pause med det samme. Login-tjek er begrænset til 5 i timen. |
| Admin | Lytter kun på 9090, som ikke er i Fly-proxyens services. Nås via `fly proxy` (WireGuard) og kræver magic link til `ADMIN_EMAIL`. Viser aldrig hemmeligheder. |
| Supply chain | `--require-hashes`. Base-image låst på digest, actions på SHA. Dependabot, CodeQL, bandit og pip-audit. |
| Container | Koden ejes af root og kan ikke skrives af appen. Processen kører som `app` med `no_new_privs` og uden capabilities. |

**Kendt begrænsning:** ForældreIntra-kodeord skal kunne dekrypteres for at
logge ind, så de kan ikke hashes. Den, der har både adgang til Fly-kontoen
(`fly ssh console`) og `MASTER_KEY`, kan læse dem. Beskyt derfor Fly- og
GitHub-kontoen med 2FA, og lad ingen andre være medlem af Fly-organisationen.

## Brudprocedure

Mistanke om læk af data, nøgler eller adgang:

1. **Stop:** `fly scale count 0` (eller pause alle forældre i admin).
2. **Rotér nøgler:**
   - Generér en ny `MASTER_KEY`, og sæt den gamle som `MASTER_KEY_OLD`. Ved
     opstart genkrypteres alt. Fjern derefter `MASTER_KEY_OLD`.
   - Skift `INVITE_CODE`, SMTP-kodeord, Anthropic-nøgle og Fly deploy-token.
3. **Invalidér sessions og kalenderlinks:**
   `fly ssh console -C "sqlite3 /data/app.db 'DELETE FROM sessions; UPDATE tenants SET ics_token_hash = NULL, ics_token_enc = NULL, ics_created_at = NULL'"`
4. **Informér forældrene**, og bed dem skifte deres ForældreIntra-kodeord
   og lave et nyt kalenderlink.
5. **Anmeld** til Datatilsynet inden for 72 timer, hvis personoplysninger
   kan være kompromitteret (databrud.dk).
6. Notér hændelsesforløbet.

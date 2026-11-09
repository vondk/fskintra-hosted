"""Kalenderfeed (iCalendar, RFC 5545): ture, ændrede tider, lektier og
afleveringer fra overblikket som heldagsbegivenheder.

Feedet hentes af en kalender-app eller -tjeneste via et hemmeligt link (se
tenants.new_ics_token). Formatet er lille, så det skrives i hånden. Teksten
stammer fra skolen, så alle værdier escapes, og linjerne foldes efter
oktetter uden at dele et UTF-8-tegn."""

import datetime
import hashlib

from . import overview

PRODID = '-//fskintra-hosted//Skoleoverblik//DA'
NAME = 'Skole'
# Hvor tit kalenderen bør hente feedet igen. De fleste bestemmer selv
REFRESH = 'PT1H'
# Hele teksten står i beskrivelsen
MAX_SUMMARY = 80
LABELS = {'tur': 'Tur/særlig dag', 'ændret_tid': 'Ændret tid',
          'lektie': 'Lektie', 'aflevering': 'Aflevering'}

# Kontroltegn (C0 og C1) fjernes - undtagen linjeskift, og en tabulator
# bliver til et mellemrum
_CONTROL = dict.fromkeys(list(range(0x20)) + list(range(0x7f, 0xa0)))
_CONTROL[0x09] = ' '
del _CONTROL[0x0a]


def escape(text):
    '''En TEXT-værdi (RFC 5545, afsnit 3.3.11)'''
    # Alle slags linjeskift bliver til ét, og øvrige kontroltegn fjernes. Et
    # råt CR/LF ville ellers begynde en ny linje - og dermed en ny egenskab
    text = '\n'.join(text.splitlines()).translate(_CONTROL)
    return (text.replace('\\', '\\\\').replace(';', '\\;')
            .replace(',', '\\,').replace('\n', '\\n'))


def fold(line):
    '''Del en linje, så ingen del fylder mere end 75 oktetter (afsnit 3.1).
    Fortsættelser begynder med et mellemrum, der tæller med'''
    raw = line.encode('utf-8')
    parts = []
    limit = 75
    while len(raw) > limit:
        cut = limit
        # Midt i et UTF-8-tegn: flyt snittet tilbage til tegnets start
        while raw[cut] & 0xC0 == 0x80:
            cut -= 1
        parts.append(raw[:cut])
        raw = raw[cut:]
        limit = 74
    parts.append(raw)
    return '\r\n '.join(p.decode('utf-8') for p in parts)


def summary(e):
    '''Fx "Oskar · Dansk: Læs side 12-14" eller "Oskar: Tur til Zoo" (som
    fskintras kalender i Home Assistant)'''
    text = e['short']
    if e['category'] == 'aflevering' and 'aflev' not in text.lower():
        text = 'Aflevering – ' + text
    who = e['child'].split()[0]
    if e['subject']:
        s = '%s · %s: %s' % (who, e['subject'], text)
    else:
        s = '%s: %s' % (who, text)
    return overview.one_line(s, MAX_SUMMARY)


def description(e):
    source = e['child']
    if e['week']:
        source += ' · ugeplan uge %d' % e['week']
    return '%s\n\n%s' % (e['text'] or e['short'], source)


def _uid(tenant_id, key, host):
    # Hashet, så hverken forælderens id eller barnets navn står i UID'et
    digest = hashlib.sha256(('%s\n%s' % (tenant_id, key)).encode('utf-8'))
    return '%s@%s' % (digest.hexdigest()[:32], host)


def _day(day):
    return day.strftime('%Y%m%d')


def build(data, today, tenant_id, host, stamp=None):
    '''Hele feedet som tekst med CRLF. data er indholdet af overblik.json
    (eller None: en tom kalender), stamp er filens tidsstempel'''
    try:
        when = datetime.datetime.fromtimestamp(int(stamp),
                                               datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        when = datetime.datetime.now(datetime.timezone.utc)
    # Filens tid og ikke "nu": så er feedet ens, til næste hentning fra
    # ForældreIntra har skrevet en ny fil
    dtstamp = when.strftime('%Y%m%dT%H%M%SZ')

    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:' + PRODID,
             'CALSCALE:GREGORIAN', 'METHOD:PUBLISH',
             'X-WR-CALNAME:' + escape(NAME),
             'REFRESH-INTERVAL;VALUE=DURATION:' + REFRESH,
             'X-PUBLISHED-TTL:' + REFRESH]
    seen = set()
    for e in overview.calendar_items(data or {}, today):
        try:
            end = e['date'] + datetime.timedelta(days=1)
        except OverflowError:
            continue
        # Punktets id er stabilt, når teksten rettes i ugeplanen, så
        # kalenderen opdaterer begivenheden i stedet for at lave en ny
        key = e['id'] or '|'.join((e['child'], _day(e['date']), e['subject'],
                                   e['category'], e['short']))
        uid = _uid(tenant_id, key, host)
        n = 1
        while uid in seen:
            n += 1
            uid = _uid(tenant_id, '%s\n%d' % (key, n), host)
        seen.add(uid)
        lines += ['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + dtstamp,
                  'DTSTART;VALUE=DATE:' + _day(e['date']),
                  'DTEND;VALUE=DATE:' + _day(end),
                  'SUMMARY:' + escape(summary(e)),
                  'DESCRIPTION:' + escape(description(e)),
                  'CATEGORIES:' + escape(LABELS[e['category']]),
                  # Heldagsbegivenheder må ikke vise forælderen som optaget
                  'TRANSP:TRANSPARENT', 'END:VEVENT']
    lines.append('END:VCALENDAR')
    return '\r\n'.join(fold(line) for line in lines) + '\r\n'

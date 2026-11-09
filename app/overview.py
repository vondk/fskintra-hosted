"""Overblikket fra ugeplanerne: lektier, afleveringer, ture, ting der skal
med og ændrede tider, fra i dag og frem.

fskintra skriver det som JSON i forælderens katalog efter hver kørsel
(FSKINTRA_OVERVIEW_JSON). Filen kommer fra en subprocess, der har behandlet
tekst fra skolen, så den regnes ikke for troværdig: den læses med en
størrelsesgrænse, hvert felt tjekkes, og teksten vises kun gennem Jinjas
autoescape.

Filen kan være ældre end i dag (pause, fejlede kørsler, natten før første
kørsel), så fortiden sorteres fra igen, hver gang den vises."""

import datetime
import json
import os
import re
import stat

from .tenants import TENANT_ID_RE

FILENAME = 'overblik.json'
VERSION = 1
MAX_BYTES = 512 * 1024
MAX_ITEMS = 500
MAX_CHILDREN = 12
# Rækker pr. barn under "Seneste ændring"
MAX_CHANGES = 6
# 'Øvrigt' (almindelig undervisning) vises kun for de nærmeste dage
INFO_DAYS = 7

SECTIONS = (
    ('Lektier og afleveringer', ('lektie', 'aflevering')),
    ('Ture og særlige dage', ('tur',)),
    ('Husk at medbringe', ('medbring',)),
    ('Ændrede tider', ('ændret_tid',)),
    ('Øvrigt', ('info',)),
)
CATEGORIES = frozenset(c for _, cats in SECTIONS for c in cats)
# Det, kalenderen viser (se ics.py)
CALENDAR_CATEGORIES = ('tur', 'ændret_tid', 'lektie', 'aflevering')
# status i filen -> (tekst, badge-klasse)
CHANGE_STATUS = {
    'tilføjet': ('Tilføjet', 'ok'),
    'ændret': ('Ændret', 'warn'),
    'fjernet': ('Fjernet', 'error'),
}
HEADLINE_WORDS = (
    ('lektie', 'lektie', 'lektier'),
    ('aflevering', 'aflevering', 'afleveringer'),
    ('tur', 'tur/særlig dag', 'ture/særlige dage'),
    ('medbring', 'ting at medbringe', 'ting at medbringe'),
    ('ændret_tid', 'ændret tid', 'ændrede tider'),
)
WEEKDAYS = ('Man', 'Tir', 'Ons', 'Tor', 'Fre', 'Lør', 'Søn')

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_WEEK_RE = re.compile(r'^(\d{1,2})-(\d{4})$')


def path(settings, tenant_id):
    if not TENANT_ID_RE.match(tenant_id or ''):
        raise ValueError('ugyldigt tenant-id')
    return os.path.join(settings.tenants_dir, tenant_id, FILENAME)


def read(settings, tenant_id):
    '''(data, mtime) for den seneste fil - eller (None, None), hvis den
    mangler, er for stor eller ikke er gyldig version 1'''
    try:
        # Kun en almindelig fil: hverken symlink ud af kataloget eller en
        # FIFO, der ville få siden til at hænge
        fd = os.open(path(settings, tenant_id),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None
    try:
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                return None, None
            raw = f.read(MAX_BYTES + 1)
    except OSError:
        return None, None
    if len(raw) > MAX_BYTES:
        return None, None
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, RecursionError):
        return None, None
    if not isinstance(data, dict) or data.get('version') != VERSION:
        return None, None
    return data, int(info.st_mtime)


def load(settings, tenant_id):
    return read(settings, tenant_id)[0]


def _text(value, limit):
    '''En streng på højst limit tegn - alt andet bliver til tom streng'''
    return value[:limit].strip() if isinstance(value, str) else ''


def _list(value):
    return value if isinstance(value, list) else []


def _date(value):
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _week(value):
    '''(ugenummer, mandag) for fx '45-2026' - eller (None, None)'''
    m = _WEEK_RE.match(value) if isinstance(value, str) else None
    if m is None:
        return None, None
    try:
        return int(m.group(1)), datetime.date.fromisocalendar(
            int(m.group(2)), int(m.group(1)), 1)
    except ValueError:
        return None, None


def one_line(text, limit=None):
    s = ' · '.join(line.strip() for line in text.splitlines()
                   if line.strip())
    if limit and len(s) > limit:
        s = s[:limit - 1].rstrip() + '…'
    return s


def fmt_day(day, today=None):
    '''Fx "Fre 6/11" - eller "I dag"/"I morgen", når today er givet'''
    if today is not None and day == today:
        return 'I dag'
    if today is not None and day == today + datetime.timedelta(days=1):
        return 'I morgen'
    return '%s %d/%d' % (WEEKDAYS[day.weekday()], day.day, day.month)


def fmt_time(value):
    '''Fx "2/11 kl. 16:35" fra unix-tid eller ISO-tidsstempel'''
    try:
        if isinstance(value, str):
            dt = datetime.datetime.fromisoformat(value)
        else:
            dt = datetime.datetime.fromtimestamp(int(value))
    except (TypeError, ValueError, OverflowError, OSError):
        return ''
    return '%d/%d kl. %02d:%02d' % (dt.day, dt.month, dt.hour, dt.minute)


def items(data, today):
    '''Gyldige punkter fra i dag og frem, sorteret efter barn og dag.

    Punkter uden dato ("Generelt" for en uge) bliver stående, til ugen er
    slut, og sorteres som ugens mandag'''
    out = []
    for p in _list(data.get('punkter'))[:MAX_ITEMS]:
        if not isinstance(p, dict):
            continue
        child = _text(p.get('barn'), 100)
        short = one_line(_text(p.get('kort'), 400))
        category = p.get('kategori')
        if not child or not short or category not in CATEGORIES:
            continue
        week_no, monday = _week(p.get('uge'))
        day = _date(p.get('dato'))
        if day is None:
            if p.get('dato') is not None or monday is None:
                continue
            if monday + datetime.timedelta(days=6) < today:
                continue
        elif day < today:
            continue
        out.append({
            'id': _text(p.get('id'), 300),
            'child': child,
            'date': day,
            'week': week_no,
            'sort': day or monday,
            'subject': _text(p.get('fag'), 100),
            'category': category,
            'short': short,
            'text': _text(p.get('tekst'), 4000),
        })
    # Uden dato før ugens dage. Rækkefølgen fra filen bevares ellers
    out.sort(key=lambda e: (e['child'], e['sort'], e['date'] is not None))
    return out


def headline(rows):
    '''Fx "2 lektier · 1 tur/særlig dag" (samme som fskintra uden AI)'''
    counts = {}
    for e in rows:
        counts[e['category']] = counts.get(e['category'], 0) + 1
    parts = ['%d %s' % (counts[c], one if counts[c] == 1 else many)
             for c, one, many in HEADLINE_WORDS if counts.get(c)]
    return ' · '.join(parts) if parts else 'Intet særligt at huske'


def _row(e, today):
    full = e['text']
    if e['date']:
        day = fmt_day(e['date'], today)
    else:
        day = 'Uge %d' % e['week']
    soon = e['date'] is not None and \
        e['date'] <= today + datetime.timedelta(days=1)
    return {
        'day': day,
        # I dag og i morgen fremhæves
        'soon': soon,
        'subject': e['subject'],
        'short': e['short'],
        'label': 'Aflevering' if e['category'] == 'aflevering' else '',
        # Hele teksten vises kun, når den siger mere end den korte
        'text': full if one_line(full) != e['short'] else '',
    }


def _children(data, rows):
    names = []
    for name in [_text(n, 100) for n in _list(data.get('boern'))] + \
            [e['child'] for e in rows]:
        if name and name not in names:
            names.append(name)
    return names[:MAX_CHILDREN]


def _changes(data):
    out = []
    for c in _list(data.get('seneste_aendring'))[:MAX_CHILDREN]:
        if not isinstance(c, dict):
            continue
        child = _text(c.get('barn'), 100)
        if not child:
            continue
        rows = []
        for r in _list(c.get('aendringer'))[:MAX_ITEMS]:
            if not isinstance(r, dict) or \
                    r.get('status') not in CHANGE_STATUS:
                continue
            day = _date(r.get('dato'))
            label, badge = CHANGE_STATUS[r['status']]
            where = (fmt_day(day) if day else '', _text(r.get('fag'), 100))
            rows.append({
                'status': label, 'badge': badge,
                'removed': r['status'] == 'fjernet',
                # Fx "Fre 6/11 · Matematik"
                'where': ' · '.join(x for x in where if x),
                'text': one_line(_text(r.get('tekst'), 4000), 160),
            })
        out.append({
            'child': child,
            'when': fmt_time(_text(c.get('tidspunkt'), 40)),
            'title': _text(c.get('titel'), 200),
            'summary': _text(c.get('resume'), 2000),
            'new': c.get('ny_ugeplan') is True,
            'rows': rows[:MAX_CHANGES],
            'more': max(0, len(rows) - MAX_CHANGES),
        })
    return out


def view(data, today, mtime=None):
    '''Alt, overblik.html skal bruge'''
    rows = items(data, today)
    info_limit = today + datetime.timedelta(days=INFO_DAYS)
    children = []
    for name in _children(data, rows):
        sections = []
        for title, cats in SECTIONS:
            mine = [e for e in rows
                    if e['child'] == name and e['category'] in cats]
            if cats == ('info',):
                mine = [e for e in mine
                        if e['date'] is None or e['date'] <= info_limit]
            if mine:
                sections.append({'title': title,
                                 'rows': [_row(e, today) for e in mine]})
        children.append({'name': name, 'sections': sections})
    # Overskriften er skrevet til den dag, filen er fra ("i morgen ...")
    fresh = data.get('dato') == today.isoformat()
    text = _text(data.get('overskrift'), 300) if fresh else ''
    return {
        'headline': text or headline(rows),
        # De korte tekster er AI-skrevne, også når filen er fra i går
        'ai': data.get('kilde') == 'ai',
        'updated': fmt_time(mtime) if mtime else '',
        'children': children,
        'changes': _changes(data),
    }


def calendar_items(data, today):
    '''Punkterne til kalenderen: kun dem med en dato'''
    return [e for e in items(data, today)
            if e['date'] is not None and
            e['category'] in CALENDAR_CATEGORIES]

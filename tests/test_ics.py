import datetime
import hashlib
import json
import os
import re
import sqlite3

import pytest

from app import db, ics, overview, tenants
from app.crypto import Crypto
from conftest import new_client, post, save_settings, signup

TODAY = datetime.date(2026, 11, 4)  # onsdag i uge 45
TID = 'a' * 32
HOST = 'hosted.example'
LINK_RE = re.compile(r'https://hosted\.example/kalender/'
                     r'([A-Za-z0-9_-]{43})\.ics')


def _item(day, category='lektie', child='Anna 3B', subject='Dansk',
          short='Læs side 12', text=None, week='45-2026', n=0):
    return {'id': '%s|%s|%s|%d' % (child, day, subject, n), 'barn': child,
            'dato': day, 'uge': week, 'fag': subject, 'kategori': category,
            'kort': short, 'tekst': short if text is None else text}


def _data(*items):
    return {'version': 1, 'dato': TODAY.isoformat(), 'kilde': 'regler',
            'opdateret': '2026-11-04T06:00:00', 'overskrift': '',
            'boern': ['Anna 3B'], 'punkter': list(items),
            'seneste_aendring': []}


def _unfold(feed):
    '''Feedets logiske linjer (RFC 5545, afsnit 3.1)'''
    assert feed.endswith('\r\n')
    return feed.replace('\r\n ', '').split('\r\n')[:-1]


def _events(feed):
    '''En dict pr. VEVENT med egenskabens navn (uden parametre) som nøgle'''
    out = []
    for line in _unfold(feed):
        name, _, value = line.partition(':')
        if line == 'BEGIN:VEVENT':
            out.append({})
        elif out and line != 'END:VEVENT' and line != 'END:VCALENDAR':
            assert name.split(';')[0] not in out[-1], 'dobbelt: ' + line
            out[-1][name.split(';')[0]] = value
    return out


def _build(*items, **kw):
    return ics.build(_data(*items), TODAY, kw.pop('tenant_id', TID), HOST,
                     kw.pop('stamp', 1793772000))


# Formatet

@pytest.mark.parametrize('pad', range(8))
def test_fold_keeps_lines_short_without_splitting_characters(pad):
    line = 'SUMMARY:' + 'x' * pad + 'Læs æøå 🎒 og rød grød med fløde ' * 9
    folded = ics.fold(line)
    parts = folded.split('\r\n')
    assert len(parts) > 3
    for part in parts:
        # .encode ville fejle på et halvt tegn - fold har allerede dekodet
        assert len(part.encode('utf-8')) <= 75
    assert all(p.startswith(' ') for p in parts[1:])
    assert folded.replace('\r\n ', '') == line


def test_fold_leaves_short_lines_alone():
    assert ics.fold('A' * 75) == 'A' * 75
    assert ics.fold('A' * 76) == 'A' * 75 + '\r\n A'


def test_escape():
    assert ics.escape('a;b,c\\d') == 'a\\;b\\,c\\\\d'
    assert ics.escape('en\r\nto\rtre\nfire fem') == \
        'en\\nto\\ntre\\nfire\\nfem'
    assert ics.escape('a\tb\x00c\x07d\x7fe\x9bf') == 'a bcdef'


def test_escape_removes_every_control_character():
    for i in list(range(0x20)) + list(range(0x7f, 0xa0)):
        want = {0x09: 'a b'}.get(i, 'ab')
        # Alt, splitlines() regner for et linjeskift, bliver til ét
        if len(('a%sb' % chr(i)).splitlines()) == 2:
            want = 'a\\nb'
        assert ics.escape('a%sb' % chr(i)) == want, hex(i)
    assert ics.escape('a\xa0b') == 'a\xa0b'


def test_text_from_the_school_cannot_add_properties_or_events():
    feed = _build(_item(
        '2026-11-05',
        short='Læs\r\nATTENDEE:mailto:ond@example.net',
        subject='Dansk\r\nLOCATION:Et andet sted',
        child='Anna 3B\nORGANIZER:mailto:ond@example.net',
        text='Hele teksten\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Falsk\n'
             'X-EKSTRA;A=B:1,2'))
    lines = _unfold(feed)
    assert lines.count('BEGIN:VEVENT') == 1 == lines.count('END:VEVENT')
    names = [line.partition(':')[0].split(';')[0] for line in lines]
    for name in ('ATTENDEE', 'LOCATION', 'ORGANIZER', 'X-EKSTRA'):
        assert name not in names
    assert names.count('SUMMARY') == 1
    assert '\r' not in feed.replace('\r\n', '')
    assert '\n' not in feed.replace('\r\n', '')
    e = _events(feed)[0]
    assert 'END:VEVENT\\nBEGIN:VEVENT' in e['DESCRIPTION']
    assert 'X-EKSTRA\\;A=B:1\\,2' in e['DESCRIPTION']


def test_feed_has_homework_trips_and_changed_times_but_nothing_else():
    feed = _build(
        _item('2026-11-05', short='Læs side 12-14; husk, bogen',
              text='Læs side 12-14 i læsebogen.\nSkriv tre linjer.'),
        _item('2026-11-06', 'aflevering', subject='Matematik',
              short='Opgave 3', n=1),
        _item('2026-11-09', 'tur', subject='', short='Tur til Zoo',
              week='46-2026'),
        _item('2026-11-10', 'ændret_tid', subject='', short='Fri kl. 12',
              week='46-2026'),
        _item('2026-11-05', 'medbring', subject='', short='Gymnastiktøj'),
        _item('2026-11-05', 'info', subject='Natur', short='Vi ser film'),
        _item(None, short='Læs 20 minutter hver dag'),
        _item('2026-11-03', short='I går'))
    events = _events(feed)
    assert [e['SUMMARY'] for e in events] == [
        'Anna · Dansk: Læs side 12-14\\; husk\\, bogen',
        'Anna · Matematik: Aflevering – Opgave 3',
        'Anna: Tur til Zoo',
        'Anna: Fri kl. 12']
    assert [e['CATEGORIES'] for e in events] == [
        'Lektie', 'Aflevering', 'Tur/særlig dag', 'Ændret tid']
    first = events[0]
    # Heldagsbegivenhed: slutdatoen er dagen efter
    assert first['DTSTART'] == '20261105' and first['DTEND'] == '20261106'
    assert first['DESCRIPTION'] == ('Læs side 12-14 i læsebogen.\\nSkriv tre '
                                    'linjer.\\n\\nAnna 3B · ugeplan uge 45')
    assert first['DTSTAMP'] == '20261104T060000Z'
    assert first['TRANSP'] == 'TRANSPARENT'
    assert 'DTSTART;VALUE=DATE:20261105\r\n' in feed
    assert 'Gymnastik' not in feed and 'film' not in feed
    assert '20 minutter' not in feed and 'I går' not in feed

    lines = _unfold(feed)
    assert lines[:2] == ['BEGIN:VCALENDAR', 'VERSION:2.0']
    assert lines[-1] == 'END:VCALENDAR'
    assert 'X-WR-CALNAME:Skole' in lines
    for part in feed.split('\r\n'):
        assert len(part.encode('utf-8')) <= 75


def test_hand_in_is_only_prefixed_when_the_text_does_not_say_so():
    events = _events(_build(
        _item('2026-11-05', 'aflevering', short='Aflevér stil'),
        _item('2026-11-06', 'aflevering', short='Stil om efteråret', n=1)))
    assert [e['SUMMARY'] for e in events] == [
        'Anna · Dansk: Aflevér stil',
        'Anna · Dansk: Aflevering – Stil om efteråret']


def test_long_summary_is_cut_but_the_description_has_it_all():
    long = 'Læs ' + 'meget ' * 40 + 'til sidst'
    e = _events(_build(_item('2026-11-05', short=long)))[0]
    assert len(e['SUMMARY']) == ics.MAX_SUMMARY and e['SUMMARY'][-1] == '…'
    assert 'til sidst' in e['DESCRIPTION']


def test_uids_are_stable_unique_and_reveal_nothing():
    same = _item('2026-11-05')
    items = [same, dict(same), _item('2026-11-06', n=1)]
    uids = [e['UID'] for e in _events(_build(*items))]
    assert len(set(uids)) == 3
    assert uids == [e['UID'] for e in _events(_build(*items, stamp=1))]
    for uid in uids:
        assert re.match(r'^[0-9a-f]{32}@hosted\.example$', uid)
        assert TID not in uid and 'Anna' not in uid
    # Teksten kan rettes, uden at kalenderen laver en ny begivenhed
    edited = _events(_build(dict(same, kort='Læs side 13')))[0]
    assert edited['UID'] == uids[0] and 'side 13' in edited['SUMMARY']
    other = _events(_build(same, tenant_id='b' * 32))[0]
    assert other['UID'] != uids[0]


def test_item_without_id_still_gets_a_uid():
    item = _item('2026-11-05')
    del item['id']
    uid = _events(_build(item, dict(item)))
    assert len({e['UID'] for e in uid}) == 2


@pytest.mark.parametrize('data', [None, {}, {'punkter': 'nej'}])
def test_empty_calendar_when_there_is_nothing(data):
    feed = ics.build(data, TODAY, TID, HOST)
    lines = _unfold(feed)
    assert lines[0] == 'BEGIN:VCALENDAR' and lines[-1] == 'END:VCALENDAR'
    assert 'BEGIN:VEVENT' not in lines


# Kalenderlinket

def _new_link(client):
    r = post(client, '/kalender/nyt')
    assert r.status_code == 303
    assert r.headers['Location'].split('?')[0].endswith('/status')
    assert r.headers['Location'].endswith('#kalender')
    m = LINK_RE.search(client.get('/status').get_data(as_text=True))
    assert m, 'intet kalenderlink på statussiden'
    return m.group(1)


def _feed(client, token):
    '''Hent feedet som en kalender-app: uden cookies'''
    return new_client(client).get('/kalender/%s.ics' % token)


def _write(ctx, tid, *items):
    today = datetime.date.today()
    path = overview.path(ctx.settings, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'version': 1, 'dato': today.isoformat(),
                   'kilde': 'regler', 'overskrift': '',
                   'opdateret': today.isoformat() + 'T06:00:00',
                   'boern': ['Anna 3B'], 'punkter': list(items),
                   'seneste_aendring': []}, f, ensure_ascii=False)


def _tomorrow():
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()


def test_no_link_until_the_parent_makes_one(client, ctx):
    tid = signup(client, ctx)
    text = client.get('/status').get_data(as_text=True)
    assert 'Lav kalenderlink' in text and '/kalender/' not in \
        text.replace('action="/kalender/nyt"', '')
    assert tenants.get(ctx.db(), tid)['ics_token_hash'] is None


def test_feed_is_served_to_a_calendar_without_login(client, ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item(_tomorrow(), short='Læs side 12-14', week=None),
           _item(_tomorrow(), 'medbring', short='Madpakke', week=None, n=1))
    token = _new_link(client)
    r = _feed(client, token)
    assert r.status_code == 200
    assert r.headers['Content-Type'] == 'text/calendar; charset=utf-8'
    assert r.headers['Cache-Control'] == 'no-store'
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    # En kalender-app skal hverken have session eller CSRF-cookie
    assert 'Set-Cookie' not in r.headers
    feed = r.get_data(as_text=True)
    assert [e['SUMMARY'] for e in _events(feed)] == [
        'Anna · Dansk: Læs side 12-14']
    assert 'Madpakke' not in feed
    assert tid not in feed and token not in feed


def test_feed_is_an_empty_calendar_before_the_first_run(client, ctx):
    signup(client, ctx)
    r = _feed(client, _new_link(client))
    assert r.status_code == 200
    assert 'BEGIN:VCALENDAR' in r.get_data(as_text=True)
    assert 'BEGIN:VEVENT' not in r.get_data(as_text=True)


def test_status_page_shows_the_link_and_how_to_subscribe(client, ctx):
    signup(client, ctx)
    token = _new_link(client)
    text = client.get('/status?m=ics_created').get_data(as_text=True)
    assert 'Kalenderlinket er klar.' in text
    assert 'value="https://hosted.example/kalender/%s.ics"' % token in text
    # webcals (https) - webcal ville sende tokenet ukrypteret først
    assert 'href="webcals://hosted.example/kalender/%s.ics"' % token in text
    assert 'webcal://' not in text
    assert 'Lav nyt link' in text and 'Slå fra' in text


def test_only_hash_and_ciphertext_are_stored(client, ctx):
    tid = signup(client, ctx)
    token = _new_link(client)
    row = tenants.get(ctx.db(), tid)
    assert row['ics_token_hash'] == hashlib.sha256(
        token.encode()).hexdigest()
    assert row['ics_token_enc'].startswith('v1.')
    assert token not in ' '.join(str(v) for v in tuple(row))
    assert row['ics_created_at'] is not None
    assert tenants.ics_token(ctx.crypto, row) == token
    assert tenants.get_by_ics_token(ctx.db(), token)['id'] == tid


def test_new_link_makes_the_old_one_stop_working(client, ctx):
    tid = signup(client, ctx)
    old = _new_link(client)
    new = _new_link(client)
    assert new != old
    assert _feed(client, old).status_code == 404
    assert _feed(client, new).status_code == 200
    events = [a['event'] for a in tenants.audit_log(ctx.db(), tid)]
    assert 'Kalenderlink oprettet' in events
    assert 'Kalenderlink fornyet' in events
    assert not any(old in e or new in e for e in events)


def test_link_can_be_turned_off(client, ctx):
    tid = signup(client, ctx)
    token = _new_link(client)
    r = post(client, '/kalender/slet')
    assert r.status_code == 303
    assert r.headers['Location'].endswith('/status?m=ics_removed#kalender')
    assert _feed(client, token).status_code == 404
    row = tenants.get(ctx.db(), tid)
    assert row['ics_token_hash'] is None and row['ics_token_enc'] is None
    assert row['ics_created_at'] is None
    text = client.get('/status?m=ics_removed').get_data(as_text=True)
    assert 'Kalenderlinket er slået fra.' in text
    assert not LINK_RE.search(text) and 'Lav kalenderlink' in text
    assert 'Kalenderlink slået fra' in [
        a['event'] for a in tenants.audit_log(ctx.db(), tid)]
    # Intet at slå fra: ingen ny hændelse
    assert post(client, '/kalender/slet').status_code == 303
    assert [a['event'] for a in tenants.audit_log(ctx.db(), tid)].count(
        'Kalenderlink slået fra') == 1


@pytest.mark.parametrize('token', [
    'x', 'A' * 43, 'A' * 42, 'A' * 44, 'A' * 42 + '.', 'A' * 42 + '%',
    'a' * 64, '..'])
def test_unknown_or_malformed_link_is_not_found(client, ctx, token):
    signup(client, ctx)
    _new_link(client)
    r = _feed(client, token)
    assert r.status_code == 404
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Set-Cookie' not in r.headers
    assert 'VCALENDAR' not in r.get_data(as_text=True)


def test_link_belongs_to_one_parent(client, ctx):
    tid_a = signup(client, ctx, 'a@example.net')
    _write(ctx, tid_a, _item(_tomorrow(), short='Annas lektie', week=None))
    token_a = _new_link(client)
    other = new_client(client)
    tid_b = signup(other, ctx, 'b@example.net')
    _write(ctx, tid_b, _item(_tomorrow(), child='Bo 5A', short='Bos lektie',
                             week=None))
    token_b = _new_link(other)
    assert token_a != token_b
    # Linket bestemmer - også når en anden forælder er logget ind
    feed_a = other.get('/kalender/%s.ics' % token_a).get_data(as_text=True)
    assert 'Annas lektie' in feed_a and 'Bos lektie' not in feed_a
    feed_b = _feed(client, token_b).get_data(as_text=True)
    assert 'Bos lektie' in feed_b and 'Annas lektie' not in feed_b


def test_deleting_the_account_kills_the_link(client, ctx):
    signup(client, ctx)
    token = _new_link(client)
    assert post(client, '/delete', {'confirm': 'SLET'}).status_code == 303
    assert _feed(client, token).status_code == 404


def test_feed_is_rate_limited_per_parent(client, ctx, monkeypatch):
    monkeypatch.setattr('app.web.ICS_FETCHES_PER_HOUR', 3)
    signup(client, ctx)
    token = _new_link(client)
    for _ in range(3):
        assert _feed(client, token).status_code == 200
    r = _feed(client, token)
    assert r.status_code == 429 and r.headers['Retry-After'] == '3600'
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'VCALENDAR' not in r.get_data(as_text=True)


def test_guessing_is_rate_limited_without_blocking_real_links(client, ctx):
    signup(client, ctx)
    token = _new_link(client)
    for _ in range(60):
        assert _feed(client, 'B' * 43).status_code == 404
    assert _feed(client, 'B' * 43).status_code == 429
    assert _feed(client, token).status_code == 200


def test_making_links_is_rate_limited(client, ctx):
    signup(client, ctx)
    for _ in range(10):
        assert post(client, '/kalender/nyt').status_code == 303
    assert post(client, '/kalender/nyt').status_code == 429


def test_link_changes_need_csrf(client, ctx):
    tid = signup(client, ctx)
    assert client.post('/kalender/nyt').status_code == 400
    assert tenants.get(ctx.db(), tid)['ics_token_hash'] is None
    token = _new_link(client)
    assert client.post('/kalender/slet').status_code == 400
    assert client.get('/kalender/nyt').status_code == 405
    assert _feed(client, token).status_code == 200


def test_link_that_cannot_be_decrypted_is_hidden_but_still_works(client,
                                                                 ctx):
    tid = signup(client, ctx)
    token = _new_link(client)
    # Som efter et skift af MASTER_KEY uden MASTER_KEY_OLD
    tenants.update(ctx.db(), tid, ics_token_enc=Crypto(b'z' * 32).encrypt(
        token, tid, 'ics_token'))
    text = client.get('/status').get_data(as_text=True)
    assert not LINK_RE.search(text) and 'webcals://' not in text
    assert 'kan ikke vises' in text and 'Lav nyt link' in text
    assert _feed(client, token).status_code == 200
    assert _new_link(client) != token


def test_encrypted_copy_must_match_the_active_link(client, ctx):
    tid = signup(client, ctx)
    _new_link(client)
    stale = 'C' * 43
    tenants.update(ctx.db(), tid, ics_token_enc=ctx.crypto.encrypt(
        stale, tid, 'ics_token'))
    assert tenants.ics_token(ctx.crypto, tenants.get(ctx.db(), tid)) == ''
    assert stale not in client.get('/status').get_data(as_text=True)


def test_export_says_there_is_a_link_but_not_what_it_is(client, ctx):
    tid = signup(client, ctx)
    konto = json.loads(client.get('/export').data)['konto']
    assert konto['kalenderlink'] is False
    assert konto['kalenderlink_oprettet'] is None
    token = _new_link(client)
    text = client.get('/export').get_data(as_text=True)
    konto = json.loads(text)['konto']
    assert konto['kalenderlink'] is True and konto['kalenderlink_oprettet']
    row = tenants.get(ctx.db(), tid)
    assert token not in text and row['ics_token_hash'] not in text
    assert row['ics_token_enc'] not in text


def test_overview_points_to_the_calendar_until_there_is_a_link(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    _write(ctx, tid, _item(_tomorrow(), week=None))
    assert 'href="/status#kalender"' in client.get('/overblik').get_data(
        as_text=True)
    _new_link(client)
    assert '/status#kalender' not in client.get('/overblik').get_data(
        as_text=True)


def test_an_error_does_not_log_the_link(client, ctx, caplog, monkeypatch):
    signup(client, ctx)
    token = _new_link(client)

    def boom(*args, **kw):
        raise RuntimeError('itu')

    monkeypatch.setattr('app.web.ics.build', boom)
    client.application.testing = False
    r = _feed(client, token)
    assert r.status_code == 500
    assert 'Exception on /kalender/<token>.ics [GET]' in caplog.text
    assert 'RuntimeError: itu' in caplog.text
    assert token not in caplog.text


# Nøgler og database

def test_key_rotation_covers_the_calendar_link(client, ctx):
    tid = signup(client, ctx)
    old_key, new_key = b'o' * 32, b'n' * 32
    token = tenants.new_ics_token(ctx.db(), Crypto(old_key), tid)
    rotating = Crypto(new_key, old_key=old_key)
    assert tenants.rotate_keys(ctx.db(), rotating) == 1
    row = tenants.get(ctx.db(), tid)
    assert tenants.ics_token(Crypto(new_key), row) == token
    assert tenants.rotate_keys(ctx.db(), rotating) == 0
    assert tenants.get_by_ics_token(ctx.db(), token)['id'] == tid


def test_migration_adds_the_calendar_columns(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'gammel.db'), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.MIGRATIONS[0] + db.MIGRATIONS[1] +
                       'PRAGMA user_version = 2;')
    for i in range(2):
        conn.execute('INSERT INTO tenants (id, email, created_at) VALUES '
                     '(?, ?, 1)', (str(i) * 32, '%d@example.net' % i))
    db.migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == \
        len(db.MIGRATIONS)
    rows = conn.execute('SELECT * FROM tenants').fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row['ics_token_hash'] is None and row['ics_token_enc'] is None
        assert row['ics_created_at'] is None
    # To forældre kan ikke have samme link
    conn.execute('UPDATE tenants SET ics_token_hash = ? WHERE id = ?',
                 ('h', '0' * 32))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('UPDATE tenants SET ics_token_hash = ? WHERE id = ?',
                     ('h', '1' * 32))
    conn.close()

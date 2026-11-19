"""Link til Home Assistant: overblikket som JSON til en anden fskintra
(--overview-from-url), der sender det videre til Home Assistant."""

import datetime
import hashlib
import json
import os
import re
import sqlite3

import pytest

from app import db, overview, tenants
from app.crypto import Crypto
from conftest import new_client, post, signup

LINK_RE = re.compile(r'https://hosted\.example/overblik/'
                     r'([A-Za-z0-9_-]{43})\.json')
ICS_RE = re.compile(r'https://hosted\.example/kalender/'
                    r'([A-Za-z0-9_-]{43})\.ics')


def _tomorrow():
    return (datetime.date.today() + datetime.timedelta(days=1)).isoformat()


def _item(child='Anna 3B', short='Læs side 12', category='lektie'):
    day = _tomorrow()
    return {'id': '%s|%s|Dansk|0' % (child, day), 'barn': child,
            'dato': day, 'uge': '45-2026', 'fag': 'Dansk',
            'kategori': category, 'kort': short, 'tekst': short}


def _write(ctx, tid, *items, **extra):
    data = {'version': 1, 'dato': datetime.date.today().isoformat(),
            'kilde': 'ai', 'overskrift': '1 lektie',
            'opdateret': '2026-11-04T06:00:00', 'boern': ['Anna 3B'],
            'punkter': list(items),
            'seneste_aendring': [{
                'barn': 'Anna 3B', 'tidspunkt': '2026-11-04T06:00:00',
                'uge': '45-2026', 'titel': 'Uge 45', 'resume': 'Ny lektie',
                'ny_ugeplan': False, 'aendringer': [{
                    'status': 'tilføjet', 'dato': _tomorrow(),
                    'fag': 'Dansk', 'tekst': 'Læs side 12'}]}]}
    data.update(extra)
    path = overview.path(ctx.settings, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def _new_link(client):
    r = post(client, '/homeassistant/nyt')
    assert r.status_code == 303
    assert r.headers['Location'].split('?')[0].endswith('/status')
    assert r.headers['Location'].endswith('#homeassistant')
    m = LINK_RE.search(client.get('/status').get_data(as_text=True))
    assert m, 'intet link til Home Assistant på statussiden'
    return m.group(1)


def _feed(client, token):
    '''Hent overblikket som fskintra: uden cookies'''
    return new_client(client).get('/overblik/%s.json' % token)


def test_no_link_until_the_parent_makes_one(client, ctx):
    tid = signup(client, ctx)
    text = client.get('/status').get_data(as_text=True)
    assert 'id="homeassistant"' in text
    assert 'Lav link til Home Assistant' in text and not LINK_RE.search(text)
    assert tenants.get(ctx.db(), tid)['feed_token_hash'] is None


def test_overview_is_served_to_fskintra_without_login(client, ctx):
    tid = signup(client, ctx)
    data = _write(ctx, tid, _item(), _item(short='Madpakke',
                                           category='medbring'))
    token = _new_link(client)
    r = _feed(client, token)
    assert r.status_code == 200
    assert r.headers['Content-Type'] == 'application/json'
    assert r.headers['Cache-Control'] == 'no-store'
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    # fskintra skal hverken have session eller CSRF-cookie
    assert 'Set-Cookie' not in r.headers
    # Præcis det, fskintra skrev - også "medbring" og seneste ændring
    assert json.loads(r.get_data(as_text=True)) == data
    assert tid not in r.get_data(as_text=True)


def test_nothing_to_send_before_the_first_run(client, ctx):
    signup(client, ctx)
    r = _feed(client, _new_link(client))
    assert r.status_code == 503
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Intet overblik endnu' in r.get_data(as_text=True)


@pytest.mark.parametrize('content', ['{"version": 2}', 'ikke json', '[1]'])
def test_an_invalid_file_is_not_sent(client, ctx, content):
    tid = signup(client, ctx)
    token = _new_link(client)
    path = overview.path(ctx.settings, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    assert _feed(client, token).status_code == 503


def test_status_page_shows_the_link_and_how_to_use_it(client, ctx):
    signup(client, ctx)
    token = _new_link(client)
    text = client.get('/status?m=feed_created').get_data(as_text=True)
    assert 'Linket til Home Assistant er klar.' in text
    url = 'https://hosted.example/overblik/%s.json' % token
    assert '<a class="copy" href="%s">%s</a>' % (url, url) in text
    assert 'FSKINTRA_OVERVIEW_URL' in text and '--overview-from-url' in text
    assert 'Lav nyt link' in text and 'Slå fra' in text
    # Beskeden står ved afsnittet og ikke øverst - og ikke ved kalenderen
    assert text.index('Linket til Home Assistant er klar.') > \
        text.index('id="homeassistant"')


def test_only_hash_and_ciphertext_are_stored(client, ctx):
    tid = signup(client, ctx)
    token = _new_link(client)
    row = tenants.get(ctx.db(), tid)
    assert row['feed_token_hash'] == hashlib.sha256(
        token.encode()).hexdigest()
    assert row['feed_token_enc'].startswith('v1.')
    assert token not in ' '.join(str(v) for v in tuple(row))
    assert row['feed_created_at'] is not None
    assert tenants.link_token(ctx.crypto, row, 'feed') == token
    assert tenants.get_by_link_token(ctx.db(), token, 'feed')['id'] == tid


def test_calendar_and_home_assistant_links_are_separate(client, ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    feed = _new_link(client)
    assert post(client, '/kalender/nyt').status_code == 303
    ics = ICS_RE.search(client.get('/status').get_data(as_text=True)).group(1)
    assert feed != ics
    # Det ene tokens link giver ikke adgang til det andet
    assert _feed(client, ics).status_code == 404
    assert new_client(client).get('/kalender/%s.ics' % feed).status_code \
        == 404
    # Det ene kan fornyes og slås fra uden at røre det andet
    new = _new_link(client)
    assert post(client, '/homeassistant/slet').status_code == 303
    assert _feed(client, new).status_code == 404
    assert new_client(client).get('/kalender/%s.ics' % ics).status_code \
        == 200
    assert tenants.get(ctx.db(), tid)['ics_token_hash'] is not None


def test_new_link_makes_the_old_one_stop_working(client, ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    old = _new_link(client)
    new = _new_link(client)
    assert new != old
    assert _feed(client, old).status_code == 404
    assert _feed(client, new).status_code == 200
    text = client.get('/status?m=feed_new').get_data(as_text=True)
    assert 'Det gamle virker ikke længere.' in text
    events = [a['event'] for a in tenants.audit_log(ctx.db(), tid)]
    assert 'Link til Home Assistant oprettet' in events
    assert 'Link til Home Assistant fornyet' in events
    assert not any(old in e or new in e for e in events)


def test_link_can_be_turned_off(client, ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    token = _new_link(client)
    r = post(client, '/homeassistant/slet')
    assert r.status_code == 303
    assert r.headers['Location'].endswith(
        '/status?m=feed_removed#homeassistant')
    assert _feed(client, token).status_code == 404
    row = tenants.get(ctx.db(), tid)
    assert row['feed_token_hash'] is None and row['feed_token_enc'] is None
    assert row['feed_created_at'] is None
    text = client.get('/status?m=feed_removed').get_data(as_text=True)
    assert 'Linket til Home Assistant er slået fra.' in text
    assert not LINK_RE.search(text) and 'Lav link til Home Assistant' in text
    # Intet at slå fra: ingen ny hændelse
    assert post(client, '/homeassistant/slet').status_code == 303
    assert [a['event'] for a in tenants.audit_log(ctx.db(), tid)].count(
        'Link til Home Assistant slået fra') == 1


@pytest.mark.parametrize('token', [
    'x', 'A' * 43, 'A' * 42, 'A' * 44, 'A' * 42 + '.', 'a' * 64, '..'])
def test_unknown_or_malformed_link_is_not_found(client, ctx, token):
    signup(client, ctx)
    _new_link(client)
    r = _feed(client, token)
    assert r.status_code == 404
    assert r.headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert 'Set-Cookie' not in r.headers


def test_link_belongs_to_one_parent(client, ctx):
    tid_a = signup(client, ctx, 'a@example.net')
    _write(ctx, tid_a, _item(short='Annas lektie'))
    token_a = _new_link(client)
    other = new_client(client)
    tid_b = signup(other, ctx, 'b@example.net')
    _write(ctx, tid_b, _item(child='Bo 5A', short='Bos lektie'))
    token_b = _new_link(other)
    # Linket bestemmer - også når en anden forælder er logget ind
    text_a = other.get('/overblik/%s.json' % token_a).get_data(as_text=True)
    assert 'Annas lektie' in text_a and 'Bos lektie' not in text_a
    text_b = _feed(client, token_b).get_data(as_text=True)
    assert 'Bos lektie' in text_b and 'Annas lektie' not in text_b


def test_the_overview_page_still_needs_login(client, ctx):
    signup(client, ctx)
    _new_link(client)
    r = new_client(client).get('/overblik')
    assert r.status_code == 302


def test_deleting_the_account_kills_the_link(client, ctx):
    signup(client, ctx)
    token = _new_link(client)
    assert post(client, '/delete', {'confirm': 'SLET'}).status_code == 303
    assert _feed(client, token).status_code == 404


def test_feed_is_rate_limited_per_parent(client, ctx, monkeypatch):
    monkeypatch.setattr('app.web.FEED_FETCHES_PER_HOUR', 3)
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    token = _new_link(client)
    for _ in range(3):
        assert _feed(client, token).status_code == 200
    r = _feed(client, token)
    assert r.status_code == 429 and r.headers['Retry-After'] == '3600'
    assert 'Læs side' not in r.get_data(as_text=True)


def test_guessing_is_rate_limited_without_blocking_real_links(client, ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    token = _new_link(client)
    for _ in range(60):
        assert _feed(client, 'B' * 43).status_code == 404
    assert _feed(client, 'B' * 43).status_code == 429
    assert _feed(client, token).status_code == 200


def test_making_links_is_rate_limited(client, ctx):
    signup(client, ctx)
    for _ in range(10):
        assert post(client, '/homeassistant/nyt').status_code == 303
    assert post(client, '/homeassistant/nyt').status_code == 429


def test_link_changes_need_login_and_csrf(client, ctx):
    tid = signup(client, ctx)
    assert client.post('/homeassistant/nyt').status_code == 400
    assert tenants.get(ctx.db(), tid)['feed_token_hash'] is None
    _new_link(client)
    assert client.post('/homeassistant/slet').status_code == 400
    assert client.get('/homeassistant/nyt').status_code == 405
    anon = new_client(client)
    assert post(anon, '/homeassistant/nyt').status_code == 302


def test_link_that_cannot_be_decrypted_is_hidden_but_still_works(client,
                                                                 ctx):
    tid = signup(client, ctx)
    _write(ctx, tid, _item())
    token = _new_link(client)
    tenants.update(ctx.db(), tid, feed_token_enc=Crypto(b'z' * 32).encrypt(
        token, tid, 'feed_token'))
    text = client.get('/status').get_data(as_text=True)
    assert not LINK_RE.search(text)
    assert 'Linket til Home Assistant kan ikke vises' in text
    assert _feed(client, token).status_code == 200
    assert _new_link(client) != token


def test_export_says_there_is_a_link_but_not_what_it_is(client, ctx):
    tid = signup(client, ctx)
    konto = json.loads(client.get('/export').data)['konto']
    assert konto['home_assistant_link'] is False
    assert konto['home_assistant_link_oprettet'] is None
    token = _new_link(client)
    text = client.get('/export').get_data(as_text=True)
    konto = json.loads(text)['konto']
    assert konto['home_assistant_link'] is True
    assert konto['home_assistant_link_oprettet']
    row = tenants.get(ctx.db(), tid)
    assert token not in text and row['feed_token_hash'] not in text
    assert row['feed_token_enc'] not in text


def test_an_error_does_not_log_the_link(client, ctx, caplog, monkeypatch):
    signup(client, ctx)
    token = _new_link(client)

    def boom(*args, **kw):
        raise RuntimeError('itu')

    monkeypatch.setattr('app.web.overview.load', boom)
    client.application.testing = False
    r = _feed(client, token)
    assert r.status_code == 500
    assert 'Exception on /overblik/<token>.json [GET]' in caplog.text
    assert token not in caplog.text


def test_privacy_page_mentions_the_link(client):
    text = client.get('/privatliv').get_data(as_text=True)
    assert 'Link til Home Assistant (valgfrit)' in text


# Nøgler og database

def test_key_rotation_covers_the_link(client, ctx):
    tid = signup(client, ctx)
    old_key, new_key = b'o' * 32, b'n' * 32
    token = tenants.new_link_token(ctx.db(), Crypto(old_key), tid, 'feed')
    rotating = Crypto(new_key, old_key=old_key)
    assert tenants.rotate_keys(ctx.db(), rotating) == 1
    row = tenants.get(ctx.db(), tid)
    assert tenants.link_token(Crypto(new_key), row, 'feed') == token
    assert tenants.rotate_keys(ctx.db(), rotating) == 0


def test_migration_adds_the_columns(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'gammel.db'), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(''.join(db.MIGRATIONS[:5]) +
                       'PRAGMA user_version = 5;')
    for i in range(2):
        conn.execute('INSERT INTO tenants (id, email, created_at) VALUES '
                     '(?, ?, 1)', (str(i) * 32, '%d@example.net' % i))
    db.migrate(conn)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == \
        len(db.MIGRATIONS)
    for row in conn.execute('SELECT * FROM tenants').fetchall():
        assert row['feed_token_hash'] is None
        assert row['feed_token_enc'] is None
        assert row['feed_created_at'] is None
    # To forældre kan ikke have samme link
    conn.execute('UPDATE tenants SET feed_token_hash = ? WHERE id = ?',
                 ('h', '0' * 32))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute('UPDATE tenants SET feed_token_hash = ? WHERE id = ?',
                     ('h', '1' * 32))
    conn.close()

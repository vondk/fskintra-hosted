import datetime
import json
import os

import pytest

from app import jobs, overview, tenants
from conftest import new_client, post, save_settings, signup

TODAY = datetime.date(2026, 11, 4)  # onsdag i uge 45


def _item(day, category='lektie', child='Anna 3B', subject='Dansk',
          short='Læs side 12', text=None, week='45-2026'):
    return {'id': '%s|%s|%s|0' % (child, day, subject), 'barn': child,
            'dato': day, 'uge': week, 'fag': subject, 'kategori': category,
            'kort': short, 'tekst': short if text is None else text}


def _data(*items, **extra):
    data = {'version': 1, 'dato': TODAY.isoformat(), 'kilde': 'regler',
            'opdateret': '2026-11-04T06:00:00', 'overskrift': '',
            'boern': ['Anna 3B'], 'punkter': list(items),
            'seneste_aendring': []}
    data.update(extra)
    return data


def _write(ctx, tid, data):
    path = overview.path(ctx.settings, tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, ensure_ascii=False)
    return path


def _today_data(*items, **extra):
    '''Som _data, men med dags dato - til sider, der bruger uret'''
    return _data(*items, dato=datetime.date.today().isoformat(), **extra)


def _in(days):
    return (datetime.date.today() + datetime.timedelta(days=days))


def _week(day):
    iso = day.isocalendar()
    return '%d-%d' % (iso[1], iso[0])


# Udvælgelse og sortering

def test_items_drop_the_past_and_keep_the_order():
    rows = overview.items(_data(
        _item('2026-11-06', short='fredag'),
        _item('2026-11-03', short='i går'),
        _item('2026-11-04', short='i dag'),
        _item(None, 'info', short='generelt'),
        _item(None, 'info', short='sidste uge', week='44-2026'),
        _item('2026-11-05', child='Bo 1A', short='bo'),
    ), TODAY)
    assert [(e['child'], e['short']) for e in rows] == [
        ('Anna 3B', 'generelt'), ('Anna 3B', 'i dag'),
        ('Anna 3B', 'fredag'), ('Bo 1A', 'bo')]
    assert rows[0]['date'] is None and rows[0]['week'] == 45


@pytest.mark.parametrize('bad', [
    'tekst', None, 17, [],
    {'barn': 'Anna', 'kort': 'x', 'kategori': 'ukendt', 'dato': '2026-11-05',
     'uge': '45-2026'},
    {'barn': '', 'kort': 'x', 'kategori': 'lektie', 'dato': '2026-11-05',
     'uge': '45-2026'},
    {'barn': 'Anna', 'kort': '', 'kategori': 'lektie', 'dato': '2026-11-05',
     'uge': '45-2026'},
    {'barn': 'Anna', 'kort': 'x', 'kategori': 'lektie', 'dato': '5/11',
     'uge': '45-2026'},
    {'barn': 'Anna', 'kort': 'x', 'kategori': 'lektie', 'dato': '2026-13-41',
     'uge': '45-2026'},
    {'barn': 'Anna', 'kort': 'x', 'kategori': 'lektie', 'dato': None,
     'uge': 'snart'},
    {'barn': 'Anna', 'kort': 'x', 'kategori': 'lektie', 'dato': None,
     'uge': '99-2026'},
    {'barn': ['Anna'], 'kort': {'a': 1}, 'kategori': ['lektie'],
     'dato': 20261105, 'uge': 45},
])
def test_items_ignore_malformed_entries(bad):
    data = _data(bad, _item('2026-11-05', short='god'))
    assert [e['short'] for e in overview.items(data, TODAY)] == ['god']


@pytest.mark.parametrize('punkter', ['x', None, {'a': 1}, 7])
def test_items_survive_a_file_without_a_list(punkter):
    assert overview.items(_data(punkter=punkter), TODAY) == []
    view = overview.view(_data(punkter=punkter, boern=punkter,
                               seneste_aendring=punkter), TODAY)
    assert view['children'] == [] and view['changes'] == []


def test_items_are_capped():
    data = _data(*[_item('2026-11-05', short='nr %d' % i)
                   for i in range(overview.MAX_ITEMS + 50)])
    assert len(overview.items(data, TODAY)) == overview.MAX_ITEMS


def test_day_labels():
    assert overview.fmt_day(TODAY, TODAY) == 'I dag'
    assert overview.fmt_day(datetime.date(2026, 11, 5), TODAY) == 'I morgen'
    assert overview.fmt_day(datetime.date(2026, 11, 6), TODAY) == 'Fre 6/11'
    assert overview.fmt_day(TODAY) == 'Ons 4/11'
    assert overview.fmt_time('2026-11-02T16:35:07') == '2/11 kl. 16:35'
    assert overview.fmt_time('i går') == ''


# Det siden viser

def test_view_groups_by_child_and_section():
    view = overview.view(_data(
        _item('2026-11-05', 'lektie', short='Læs', text='Læs\nside 12-14'),
        _item('2026-11-06', 'aflevering', subject='', short='Stil'),
        _item('2026-11-10', 'tur', short='Museum'),
        _item('2026-11-09', 'medbring', short='Idrætstøj'),
        _item('2026-11-09', 'ændret_tid', short='Fri kl. 12'),
        _item('2026-11-11', 'info', short='Brøker'),
        _item('2026-11-12', 'info', short='for langt ude'),
        _item(None, 'info', short='Generelt for ugen'),
        boern=['Anna 3B', 'Bo 1A']), TODAY)
    anna, bo = view['children']
    assert [s['title'] for s in anna['sections']] == [
        'Lektier og afleveringer', 'Ture og særlige dage',
        'Husk at medbringe', 'Ændrede tider', 'Øvrigt']
    homework = anna['sections'][0]['rows']
    assert homework[0] == {
        'day': 'I morgen', 'soon': True, 'subject': 'Dansk', 'short': 'Læs',
        'label': '', 'text': 'Læs\nside 12-14'}
    assert homework[1]['label'] == 'Aflevering'
    assert homework[1]['soon'] is False and homework[1]['text'] == ''
    assert [r['short'] for r in anna['sections'][4]['rows']] == [
        'Generelt for ugen', 'Brøker']
    assert anna['sections'][4]['rows'][0]['day'] == 'Uge 45'
    # Et barn uden punkter er stadig med ("Intet de næste dage")
    assert bo == {'name': 'Bo 1A', 'sections': []}
    assert view['headline'] == ('1 lektie · 1 aflevering · 1 tur/særlig dag '
                                '· 1 ting at medbringe · 1 ændret tid')


def test_headline_from_the_file_only_on_the_day_it_was_written():
    data = _data(_item('2026-11-06'), overskrift='I morgen: husk stil',
                 kilde='ai')
    assert overview.view(data, TODAY)['headline'] == 'I morgen: husk stil'
    later = overview.view(data, TODAY + datetime.timedelta(days=1))
    assert later['headline'] == '1 lektie' and later['ai'] is True
    empty = overview.view(_data(), TODAY)
    assert empty['headline'] == 'Intet særligt at huske'
    assert empty['ai'] is False


def test_changes():
    rows = [{'status': 'tilføjet', 'dato': '2026-11-06', 'fag': 'Dansk',
             'tekst': 'Ny\nlektie'},
            {'status': 'fjernet', 'dato': None, 'fag': '', 'tekst': 'Væk'},
            {'status': 'sprængt', 'dato': None, 'fag': '', 'tekst': 'x'},
            'ikke en række']
    rows += [{'status': 'ændret', 'dato': '2026-11-05', 'fag': '',
              'tekst': 'nr %d' % i} for i in range(7)]
    changes = overview.view(_data(seneste_aendring=[
        {'barn': 'Anna 3B', 'tidspunkt': '2026-11-02T16:35:00',
         'uge': '45-2026', 'titel': 'Uge 45', 'resume': 'Ny lektie i dansk',
         'ny_ugeplan': False, 'aendringer': rows},
        {'barn': 'Bo 1A', 'tidspunkt': None, 'titel': None, 'resume': None,
         'ny_ugeplan': True, 'aendringer': []},
        {'barn': None}, 'skrammel',
    ]), TODAY)['changes']
    anna, bo = changes
    assert anna['when'] == '2/11 kl. 16:35' and anna['title'] == 'Uge 45'
    assert anna['summary'] == 'Ny lektie i dansk' and anna['new'] is False
    assert len(anna['rows']) == overview.MAX_CHANGES and anna['more'] == 3
    assert anna['rows'][0] == {
        'status': 'Tilføjet', 'badge': 'ok', 'removed': False,
        'where': 'Fre 6/11 · Dansk', 'text': 'Ny · lektie'}
    assert anna['rows'][1]['removed'] is True
    assert anna['rows'][1]['where'] == ''
    assert bo == {'child': 'Bo 1A', 'when': '', 'title': '', 'summary': '',
                  'new': True, 'rows': [], 'more': 0}


def test_calendar_items_have_a_date_and_a_calendar_category():
    rows = overview.calendar_items(_data(
        _item('2026-11-05', 'lektie', short='a'),
        _item('2026-11-05', 'aflevering', short='b'),
        _item('2026-11-05', 'tur', short='c'),
        _item('2026-11-05', 'ændret_tid', short='d'),
        _item('2026-11-05', 'medbring', short='e'),
        _item('2026-11-05', 'info', short='f'),
        _item(None, 'tur', short='g'),
        _item('2026-11-03', 'tur', short='h')), TODAY)
    assert [e['short'] for e in rows] == ['a', 'b', 'c', 'd']


# Filen er ikke troværdig

def test_read_missing_broken_or_foreign_files(ctx, tmp_path):
    tid = tenants.create(ctx.db(), 'mor@example.net')
    assert overview.read(ctx.settings, tid) == (None, None)
    path = _write(ctx, tid, _data())
    data, mtime = overview.read(ctx.settings, tid)
    assert data['version'] == 1 and mtime == int(os.stat(path).st_mtime)

    for bad in ('{"version": 1', '[1, 2]', '"tekst"', 'null',
                json.dumps({'version': 2, 'punkter': []}),
                json.dumps({'punkter': []}),
                '[' * 100000):
        _write(ctx, tid, bad)
        assert overview.read(ctx.settings, tid) == (None, None), bad[:20]

    with open(path, 'wb') as f:
        f.write(b'\xff\xfe{"version": 1}')
    assert overview.load(ctx.settings, tid) is None

    big = _data(overskrift='x' * overview.MAX_BYTES)
    _write(ctx, tid, big)
    assert overview.load(ctx.settings, tid) is None

    # Et symlink ud af kataloget følges ikke
    outside = tmp_path / 'andet.json'
    outside.write_text(json.dumps(_data()), encoding='utf-8')
    os.unlink(path)
    os.symlink(str(outside), path)
    assert overview.load(ctx.settings, tid) is None

    # En FIFO må ikke få siden til at hænge
    os.unlink(path)
    os.mkfifo(path)
    assert overview.load(ctx.settings, tid) is None

    with pytest.raises(ValueError):
        overview.path(ctx.settings, '../andet')


# Siden

def test_login_lands_on_the_overview(client, ctx):
    signup(client, ctx)
    save_settings(client)
    r = client.get('/')
    assert r.status_code == 302 and r.headers['Location'].endswith(
        '/overblik')
    assert client.get('/kode').headers['Location'].endswith('/overblik')
    post(client, '/logout')
    post(client, '/login', {'email': 'foraelder@example.net'})
    r = post(client, '/auth/' + ctx.mailer.last_link())
    assert r.status_code == 303 and r.headers['Location'].endswith(
        '/overblik')
    # "Log ud på alle andre enheder" bliver på siden med knappen
    r = post(client, '/logout-all')
    assert r.status_code == 303 and r.headers['Location'].endswith('/status')


def test_new_account_is_sent_to_the_settings(client, ctx):
    signup(client, ctx)
    r = client.get('/overblik')
    text = r.data.decode('utf-8')
    assert r.status_code == 200 and 'Angiv login' in text
    assert 'kører snart' not in text


def test_page_before_the_first_run(client, ctx):
    signup(client, ctx)
    save_settings(client)
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Første hentning kører snart' in text
    assert '<a href="/overblik" aria-current="page">Overblik</a>' in text


def test_page_after_a_run(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    jobs.execute(ctx, tid, 'catchup')
    r = client.get('/overblik')
    text = r.data.decode('utf-8')
    assert r.status_code == 200 and r.headers['Cache-Control'] == 'no-store'
    assert '<p class="lead">1 lektie</p>' in text
    assert 'Anna 3B</h2>' in text and 'Lektier og afleveringer</h3>' in text
    assert '<span class="day soon">I morgen</span>' in text
    assert '<strong>Dansk:</strong> Læs side 12-14' in text
    assert 'Senest hentet ' in text and 'AI-resumé' not in text
    assert 'Vis hele teksten' not in text and 'Seneste ændring' not in text
    assert 'class="warn"' not in text


def test_page_escapes_text_from_the_school(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    day = _in(2)
    evil = '<script>alert(1)</script>'
    _write(ctx, tid, _today_data(
        _item(day.isoformat(), child='Anna <b>3B</b>', subject=evil,
              short=evil, text=evil + '\n<img src=x onerror=alert(1)>',
              week=_week(day)),
        boern=['Anna <b>3B</b>'], overskrift=evil, kilde='ai',
        seneste_aendring=[{
            'barn': evil, 'tidspunkt': evil, 'titel': evil, 'resume': evil,
            'ny_ugeplan': False, 'aendringer': [{
                'status': 'ændret', 'dato': day.isoformat(), 'fag': evil,
                'tekst': evil}]}]))
    text = client.get('/overblik').data.decode('utf-8')
    assert '<script' not in text and '<img src=x' not in text
    assert '<b>3B' not in text
    assert text.count('&lt;script&gt;alert(1)&lt;/script&gt;') >= 7
    assert 'Vis hele teksten' in text and 'AI-resumé' in text
    assert 'Seneste ændring' in text
    assert '<span class="badge warn">Ændret</span>' in text


def test_page_hides_what_is_over(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    past, soon = _in(-1), _in(3)
    data = _today_data(
        _item(past.isoformat(), short='Overstået lektie', week=_week(past)),
        _item(soon.isoformat(), 'tur', short='Tur til museet',
              week=_week(soon)),
        boern=['Anna 3B', 'Bo 1A'])
    # Filen er fra i går: overskriften i den gælder ikke længere
    data.update(dato=past.isoformat(), overskrift='I morgen: lektie')
    _write(ctx, tid, data)
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Overstået lektie' not in text and 'I morgen: lektie' not in text
    assert 'Tur til museet' in text
    assert '<p class="lead">1 tur/særlig dag</p>' in text
    assert 'Bo 1A</h2>' in text and 'Intet de næste dage.' in text


@pytest.mark.parametrize('content', [
    '{"version": 1', json.dumps({'version': 7}), json.dumps(['x'])])
def test_page_with_a_broken_file(client, ctx, content):
    tid = signup(client, ctx)
    save_settings(client)
    _write(ctx, tid, content)
    r = client.get('/overblik')
    assert r.status_code == 200
    assert 'Første hentning kører snart' in r.data.decode('utf-8')


def test_page_without_any_weekplans_yet(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    tenants.update(ctx.db(), tid, needs_catchup=0)
    assert 'klar efter næste hentning' in client.get(
        '/overblik').data.decode('utf-8')
    _write(ctx, tid, _today_data(boern=[]))
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Intet særligt at huske' in text
    assert 'Der er ikke hentet nogen ugeplaner endnu.' in text


def test_page_explains_why_it_is_stale(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    jobs.execute(ctx, tid, 'catchup')

    tenants.update(ctx.db(), tid, last_exit=4)
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Den seneste hentning fejlede' in text and 'Læs side 12' in text

    post(client, '/pause')
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Hentningen er på pause' in text and 'Du har selv sat' in text
    assert 'Den seneste hentning fejlede' not in text
    assert 'Læs side 12' in text  # det gamle overblik vises stadig

    os.unlink(overview.path(ctx.settings, tid))
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Hentningen er på pause' in text and 'kører snart' not in text


def test_page_says_when_weekplans_are_off(client, ctx):
    signup(client, ctx)
    save_settings(client, section_pln=None)
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Slå "Ugeplaner" til' in text or \
        'Slå &#34;Ugeplaner&#34; til' in text
    assert 'kører snart' not in text


def test_failed_run_keeps_the_last_overview(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    jobs.execute(ctx, tid, 'catchup')
    tenants.update(ctx.db(), tid, username='nede')
    jobs.execute(ctx, tid, 'run')
    assert tenants.get(ctx.db(), tid)['last_exit'] == 4
    text = client.get('/overblik').data.decode('utf-8')
    assert 'Læs side 12-14' in text and 'hentning fejlede' in text


def test_export_includes_the_overview(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    assert json.loads(client.get('/export').data)['overblik'] is None
    jobs.execute(ctx, tid, 'catchup')
    data = json.loads(client.get('/export').data)
    assert data['overblik']['punkter'][0]['kort'] == 'Læs side 12-14'


def test_overview_is_private_to_the_account(client, ctx):
    tid = signup(client, ctx)
    save_settings(client)
    jobs.execute(ctx, tid, 'catchup')
    other = new_client(client)
    signup(other, ctx, 'anden@example.net')
    save_settings(other, username='andenbruger')
    text = other.get('/overblik').data.decode('utf-8')
    assert 'Læs side 12-14' not in text
    assert json.loads(other.get('/export').data)['overblik'] is None

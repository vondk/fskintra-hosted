"""Falsk fskintra til tests. Opførslen styres af brugernavnet, og miljøet
gemmes i FSKINTRA_ROOT/env.json, så tests kan se præcis hvad der sendes"""
import datetime
import json
import os
import shutil
import sys
import time

root = os.environ['FSKINTRA_ROOT']
with open(os.path.join(root, 'env.json'), 'w') as f:
    json.dump({'env': dict(os.environ), 'args': sys.argv[1:]}, f)

if '--purge' in sys.argv:
    # Som det rigtige fskintra: slet alt gemt og skriv, hvad der blev slettet
    purged = []
    for name in ('cache', 'msg', 'state'):
        path = os.path.join(root, name)
        if os.path.lexists(path):
            shutil.rmtree(path)
            purged.append(path)
    path = os.path.join(root, os.environ.get('FSKINTRA_OVERVIEW_JSON', ''))
    if os.path.isfile(path):
        os.unlink(path)
        purged.append(path)
    print(json.dumps({'purged': purged}))
    sys.exit(0)

user = os.environ.get('FSKINTRA_USERNAME', '')
if user == 'forkert':
    sys.stderr.write('Log ind giver en fejlmeddelse\n')
    sys.exit(3)
if user == 'nede':
    sys.stderr.write('skoleLogin: Kan ikke logge på ForældreIntra?\n')
    sys.exit(4)
if user == 'sover':
    time.sleep(60)
if user == 'laekker':
    # Fast markør - testen bruger den som kodeord og tjekker at den fjernes
    # fra log-halen (fixturen læser aldrig det rigtige kodeord)
    sys.stderr.write('fejl: LAEKKET-MARKOER-4711\n')
    sys.exit(4)

if '--check-login' in sys.argv:
    print(json.dumps({'children': ['Anna 3B']}))
    sys.exit(0)

os.makedirs(os.path.join(root, 'cache'), exist_ok=True)
with open(os.path.join(root, 'cache', 'side.cache'), 'w') as f:
    f.write('indhold')

# Overblikket, som det rigtige fskintra skriver det efter en hentning
overview = os.environ.get('FSKINTRA_OVERVIEW_JSON')
if overview:
    today = datetime.date.today()
    day = today + datetime.timedelta(days=1)
    week = '%d-%d' % (day.isocalendar()[1], day.isocalendar()[0])
    with open(os.path.join(root, overview), 'w', encoding='utf-8') as f:
        json.dump({
            'version': 1, 'dato': today.isoformat(), 'kilde': 'regler',
            'opdateret': today.isoformat() + 'T06:00:00',
            'overskrift': '1 lektie', 'boern': ['Anna 3B'],
            'punkter': [{
                'id': 'Anna 3B|%s|Dansk|0' % day.isoformat(),
                'barn': 'Anna 3B', 'dato': day.isoformat(), 'uge': week,
                'fag': 'Dansk', 'kategori': 'lektie',
                'kort': 'Læs side 12-14', 'tekst': 'Læs side 12-14',
            }],
            'seneste_aendring': [],
        }, f)
sys.exit(0)

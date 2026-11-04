"""Falsk fskintra til tests. Opførslen styres af brugernavnet, og miljøet
gemmes i FSKINTRA_ROOT/env.json, så tests kan se præcis hvad der sendes"""
import json
import os
import sys
import time

root = os.environ['FSKINTRA_ROOT']
with open(os.path.join(root, 'env.json'), 'w') as f:
    json.dump({'env': dict(os.environ), 'args': sys.argv[1:]}, f)

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
sys.exit(0)

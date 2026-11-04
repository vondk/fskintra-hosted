"""Fælles for skabelonerne i den offentlige app og admin-appen"""

import hashlib
import os


def install(app):
    '''asset('style.css') giver /static/style.css?v=<hash>, så browsere
    henter de nye filer efter et deploy i stedet for at bruge en gammel
    kopi fra cachen'''
    versions = {}

    def asset(name):
        if name not in versions:
            with open(os.path.join(app.static_folder, name), 'rb') as f:
                versions[name] = hashlib.sha256(f.read()).hexdigest()[:12]
        return '/static/%s?v=%s' % (name, versions[name])

    app.jinja_env.globals['asset'] = asset

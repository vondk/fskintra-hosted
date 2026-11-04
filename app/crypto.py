"""Kryptering af hemmeligheder (ForældreIntra-kodeord, egne API-nøgler).

AES-256-GCM med en nøgle der kun findes i Fly secrets (MASTER_KEY).

* AAD = "<tenant-id>:<felt>" binder en ciphertext til præcis én række og
  ét felt - en ciphertext kan ikke flyttes over på en anden forælder.
* Formatet er "v1.<nøgle-id>.<nonce>.<ciphertext>" (base64url). Nøgle-id er
  de første 8 hex-tegn af SHA-256(nøgle), så rotation (MASTER_KEY_OLD) kan
  afgøre hvilken nøgle en værdi er krypteret med.
"""

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 'v1'


class DecryptError(Exception):
    pass


def _b64e(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')


def _b64d(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def key_id(key):
    return hashlib.sha256(key).hexdigest()[:8]


def aad(tenant_id, field):
    return ('%s:%s' % (tenant_id, field)).encode('utf-8')


class Crypto:
    def __init__(self, key, old_key=None):
        if len(key) != 32:
            raise ValueError('nøglen skal være 32 bytes')
        self._current = (key_id(key), AESGCM(key))
        self._keys = {self._current[0]: self._current[1]}
        if old_key:
            self._keys.setdefault(key_id(old_key), AESGCM(old_key))

    def encrypt(self, plaintext, tenant_id, field):
        kid, aes = self._current
        nonce = os.urandom(12)
        ct = aes.encrypt(nonce, plaintext.encode('utf-8'),
                         aad(tenant_id, field))
        return '.'.join((VERSION, kid, _b64e(nonce), _b64e(ct)))

    def decrypt(self, token, tenant_id, field):
        try:
            version, kid, nonce, ct = token.split('.')
        except (AttributeError, ValueError):
            raise DecryptError('ugyldigt format')
        if version != VERSION or kid not in self._keys:
            raise DecryptError('ukendt version eller nøgle')
        try:
            pt = self._keys[kid].decrypt(_b64d(nonce), _b64d(ct),
                                         aad(tenant_id, field))
        except (InvalidTag, ValueError):
            raise DecryptError('kunne ikke dekrypteres')
        return pt.decode('utf-8')

    def needs_rotation(self, token):
        parts = (token or '').split('.')
        return len(parts) == 4 and parts[1] != self._current[0]

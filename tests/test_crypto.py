import pytest

from app.crypto import Crypto, DecryptError

A, B = b'a' * 32, b'b' * 32


def test_roundtrip_and_random_nonce():
    c = Crypto(A)
    t1 = c.encrypt('hemmelig kode æøå', 'tenant1', 'password')
    t2 = c.encrypt('hemmelig kode æøå', 'tenant1', 'password')
    assert t1 != t2  # ny nonce hver gang
    assert 'hemmelig' not in t1
    assert c.decrypt(t1, 'tenant1', 'password') == 'hemmelig kode æøå'


@pytest.mark.parametrize('tenant,field', [('tenant2', 'password'),
                                          ('tenant1', 'ai_key')])
def test_ciphertext_is_bound_to_tenant_and_field(tenant, field):
    c = Crypto(A)
    token = c.encrypt('x', 'tenant1', 'password')
    with pytest.raises(DecryptError):
        c.decrypt(token, tenant, field)


def test_tampering_is_detected():
    c = Crypto(A)
    v, kid, nonce, ct = c.encrypt('x', 't', 'password').split('.')
    bad = '.'.join((v, kid, nonce, ('A' if ct[0] != 'A' else 'B') + ct[1:]))
    with pytest.raises(DecryptError):
        c.decrypt(bad, 't', 'password')
    with pytest.raises(DecryptError):
        c.decrypt('ikke-et-token', 't', 'password')


def test_key_rotation():
    old = Crypto(A).encrypt('kode', 't', 'password')
    rotating = Crypto(B, old_key=A)
    assert rotating.needs_rotation(old)
    assert rotating.decrypt(old, 't', 'password') == 'kode'
    new = rotating.encrypt('kode', 't', 'password')
    assert not rotating.needs_rotation(new)
    with pytest.raises(DecryptError):
        Crypto(B).decrypt(old, 't', 'password')

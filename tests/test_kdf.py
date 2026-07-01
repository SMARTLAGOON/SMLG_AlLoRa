"""Unit — deriving the session keys from the ECDH shared secret.

The handshake's shared secret is a raw curve coordinate, not usable key material directly.
The KDF stretches and separates it into the three things a session needs: an AES encryption
key, a distinct HMAC key, and a per-session nonce prefix. Using HKDF-SHA256 (a standard
extract-then-expand KDF) means the three outputs are independent even though they come from
one secret.

These are *property* tests — deterministic, right lengths, the outputs are distinct, and a
different secret yields different keys. They deliberately do not pin the exact bytes: the
concrete construction (labels, prefix length) is provisional until the crypto-review pass,
and property tests survive that review unchanged.
"""
from AlLoRa.Security.kdf import derive_session_keys, ENC_KEY_LEN, MAC_KEY_LEN

SECRET = bytes(range(32))
OTHER = bytes(range(1, 33))


def test_keys_are_the_expected_lengths():
    enc, mac, prefix = derive_session_keys(SECRET)
    assert len(enc) == ENC_KEY_LEN == 16
    assert len(mac) == MAC_KEY_LEN == 16
    assert len(prefix) > 0


def test_derivation_is_deterministic():
    assert derive_session_keys(SECRET) == derive_session_keys(SECRET)


def test_the_three_outputs_are_distinct():
    enc, mac, prefix = derive_session_keys(SECRET)
    assert enc != mac
    assert enc != prefix[:ENC_KEY_LEN]
    assert mac != prefix[:MAC_KEY_LEN]


def test_a_different_secret_yields_different_keys():
    enc1, mac1, p1 = derive_session_keys(SECRET)
    enc2, mac2, p2 = derive_session_keys(OTHER)
    assert (enc1, mac1, p1) != (enc2, mac2, p2)
    assert enc1 != enc2 and mac1 != mac2

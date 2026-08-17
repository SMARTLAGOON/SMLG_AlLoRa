"""Unit — deriving the session keys from the ECDH shared secret.

The handshake's shared secret is a raw curve coordinate, not usable key material directly.
The KDF stretches and separates it into the three things a session needs: an AES encryption
key, a distinct HMAC key, and a per-session nonce prefix. Using HKDF-SHA256 (a standard
extract-then-expand KDF) means the three outputs are independent even though they come from
one secret.

These are mostly *property* tests — deterministic, right lengths, the outputs are distinct, and
a different secret yields different keys. They deliberately do not pin the exact bytes of the
session keys: the concrete construction (labels, prefix length) is provisional until the
crypto-review pass, and property tests survive that review unchanged.

``hkdf_sha256`` itself is the exception and *is* byte-pinned, against the RFC 5869 vectors.
HKDF-SHA256 is a standard rather than a provisional choice, so those bytes cannot move under
the review, and pinning them is what catches a broken HMAC underneath: every property above
would still hold if the MAC were replaced with something fast and wrong.
"""
import hmac

from AlLoRa.Security.kdf import (
    derive_session_keys, derive_session_material, hkdf_sha256, ENC_KEY_LEN, MAC_KEY_LEN,
)

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


def test_directional_material_gives_two_disjoint_prefixes():
    # One shared key pair, but a distinct nonce prefix per direction so the two directions'
    # nonce spaces never overlap (the fix for cross-direction (key, nonce) reuse).
    enc, mac, prefix_ab, prefix_ba = derive_session_material(SECRET)
    assert len(enc) == ENC_KEY_LEN and len(mac) == MAC_KEY_LEN
    assert len(prefix_ab) == len(prefix_ba) > 0
    assert prefix_ab != prefix_ba
    # the shared keys still match what the single-set derivation would give up front
    assert (enc, mac) == derive_session_keys(SECRET)[:2]


def test_directional_material_is_deterministic():
    assert derive_session_material(SECRET) == derive_session_material(SECRET)


def test_hkdf_matches_rfc_5869_test_case_1():
    # The standard's own vector, with a real salt and a real info label.
    okm = hkdf_sha256(bytes.fromhex("0b" * 22), 42,
                      salt=bytes.fromhex("000102030405060708090a0b0c"),
                      info=bytes.fromhex("f0f1f2f3f4f5f6f7f8f9"))
    assert okm.hex() == ("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56"
                         "ecc4c5bf34007208d5b887185865")


def test_hkdf_matches_rfc_5869_test_case_3():
    # Zero-length salt and info, which is the branch this module actually takes: nothing here
    # passes a salt, so the empty-salt substitution (32 zero bytes) is on the live path and
    # would otherwise go unpinned.
    okm = hkdf_sha256(bytes.fromhex("0b" * 22), 42)
    assert okm.hex() == ("8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f"
                         "3c738d2d9d201395faa4b61a96c8")


def test_the_key_schedule_does_not_call_into_the_hmac_module(monkeypatch):
    # HKDF is charged one extract plus one hash pass per 32 bytes of output, so a handshake pays
    # micropython-lib's pure-Python HMAC three times: about 58 ms on the Edge and 144 ms on the
    # Hub. Less than the per-frame tag was costing, but on the same path and free to remove once
    # the package had its own HMAC.
    expected = derive_session_material(SECRET)      # before the module is taken away

    def _unavailable(*args, **kwargs):
        raise AssertionError("the key schedule must not go through the hmac module")

    monkeypatch.setattr(hmac, "new", _unavailable)
    assert derive_session_material(SECRET) == expected

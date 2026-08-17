"""Unit — the package's own HMAC-SHA256 (RFC 2104) over the native hash.

Two callers depend on this returning exactly what the standard library returns: the per-frame
AEAD tag and the session key schedule. It exists at all because MicroPython has no native HMAC,
so the alternative is micropython-lib's pure-Python wrapper, which costs 19.4 ms on the Edge and
48.1 ms on the Hub against 0.14 ms for the SHA-256 it wraps.

That makes these tests unusual in one way worth stating: the point of the change was speed, and
none of these measure it. A timing test would run on CPython in CI, where the pure-Python module
was never the bottleneck, so it would pass whatever the code did. What CI *can* prove is the part
that would be catastrophic to get wrong, which is that a faster MAC is still the same MAC.
"""
import hashlib
import hmac

from AlLoRa.Security.hmac_sha256 import hmac_sha256, BLOCK_SIZE, DIGEST_SIZE


def _reference(key, msg):
    return hmac.new(bytes(key), bytes(msg), hashlib.sha256).digest()


def test_it_matches_the_standard_library_across_key_and_message_lengths():
    # The key lengths straddle the 64-byte block boundary on purpose. RFC 2104 hashes a key
    # longer than one block before padding it, and that branch is the one an open-coded HMAC
    # gets wrong: a version that skipped it would agree with the reference on every key a
    # session actually uses (16 bytes) and diverge only on the path nothing exercises.
    for key_len in (0, 1, 16, 32, 63, BLOCK_SIZE, 65, 100, 200):
        key = bytes((i * 7 + 1) % 256 for i in range(key_len))
        for msg_len in (0, 1, 8, 55, 64, 200, 247, 1000):
            msg = bytes(i % 256 for i in range(msg_len))
            assert hmac_sha256(key, msg) == _reference(key, msg), \
                "key_len={} msg_len={}".format(key_len, msg_len)


def test_it_returns_a_full_length_digest():
    # Callers truncate themselves (the AEAD tag takes 4 bytes), so this must hand back all 32.
    assert len(hmac_sha256(b"k", b"m")) == DIGEST_SIZE == 32


def test_it_accepts_bytearrays_as_well_as_bytes():
    # The AEAD is handed keys and nonces that are sometimes bytearrays, so neither argument may
    # assume immutability.
    assert hmac_sha256(bytearray(b"key"), bytearray(b"msg")) == _reference(b"key", b"msg")


def test_a_changed_key_or_message_changes_the_digest():
    base = hmac_sha256(b"key", b"message")
    assert hmac_sha256(b"kex", b"message") != base
    assert hmac_sha256(b"key", b"messagf") != base


def test_it_does_not_call_into_the_hmac_module(monkeypatch):
    # The whole point. Asserting the dependency is gone is the only check CI can make that would
    # actually fail if someone reintroduced the pure-Python wrapper, since on CPython that
    # wrapper is fast and every other test would stay green.
    expected = hmac_sha256(b"key", b"message")      # before the module is taken away

    def _unavailable(*args, **kwargs):
        raise AssertionError("hmac_sha256 must not go through the hmac module")

    monkeypatch.setattr(hmac, "new", _unavailable)
    assert hmac_sha256(b"key", b"message") == expected

"""Unit — the per-frame AEAD backend (AES-128-CTR + truncated HMAC-SHA256).

Secure mode seals each frame with authenticated encryption: encrypt-then-MAC using native
AES-CTR and SHA-256, with a 4-byte tag. The backend is a *pure crypto primitive* — it is
handed already-assembled ``nonce`` and ``aad`` bytes and never knows their layout, so the
(still-provisional) nonce/AAD byte layout lives at the call site, not here. That keeps
these tests about crypto *properties* — round-trips, tamper is caught, wrong key/nonce is
caught — which survive a later byte-layout review unchanged.

``open()`` returns the plaintext on success and ``None`` on any authentication failure; it
verifies the tag *before* decrypting, so a forged frame never yields plaintext. Tests run
against the platform-detected backend (``detect_aead()``), exercising the real path.
"""
from AlLoRa.Security.AEAD import detect_aead, Ctr_hmac_aead, _self_test

ENC_KEY = bytes(range(16))          # AES-128 key
MAC_KEY = bytes(range(16, 32))      # separate HMAC key
NONCE = bytes(range(12))            # assembled elsewhere (prefix + counter); opaque here
AAD = b"header-bytes-bound-by-the-mac"
PLAINTEXT = bytes(i % 256 for i in range(243))  # a full chunk


def _sealed():
    aead = detect_aead()
    assert aead is not None, "no AEAD backend on this platform"
    return aead, aead.seal(ENC_KEY, MAC_KEY, NONCE, AAD, PLAINTEXT)


def test_seal_then_open_round_trips_the_plaintext():
    aead, sealed = _sealed()
    assert aead.open(ENC_KEY, MAC_KEY, NONCE, AAD, sealed) == PLAINTEXT


def test_a_tampered_ciphertext_is_rejected():
    aead, sealed = _sealed()
    forged = bytearray(sealed)
    forged[0] ^= 0x01                      # flip a bit in the ciphertext
    assert aead.open(ENC_KEY, MAC_KEY, NONCE, AAD, bytes(forged)) is None


def test_a_tampered_tag_is_rejected():
    aead, sealed = _sealed()
    forged = bytearray(sealed)
    forged[-1] ^= 0x01                     # flip a bit in the 4-byte tag
    assert aead.open(ENC_KEY, MAC_KEY, NONCE, AAD, bytes(forged)) is None


def test_a_forged_header_is_rejected():
    # The AAD carries the frame header (kind, sid, counter, ...). Changing it must break the
    # tag even though the ciphertext is untouched -> the header cannot be forged.
    aead, sealed = _sealed()
    assert aead.open(ENC_KEY, MAC_KEY, NONCE, AAD + b"!", sealed) is None


def test_another_sessions_keys_do_not_open_the_frame():
    # enc and mac keys are always derived together from one shared secret, so "wrong key"
    # means a different session's key material -> the MAC (keyed by the mac key) fails and
    # nothing decrypts. (A wrong enc key alone is not a real scenario in encrypt-then-MAC:
    # the mac key, not the enc key, is what authenticates.)
    aead, sealed = _sealed()
    other_enc = bytes((b ^ 0xFF) for b in ENC_KEY)
    other_mac = bytes((b ^ 0xFF) for b in MAC_KEY)
    assert aead.open(other_enc, other_mac, NONCE, AAD, sealed) is None


def test_a_wrong_nonce_is_rejected():
    # The nonce is bound by the MAC, so replaying a frame's bytes under a different counter
    # (a different nonce) fails authentication.
    aead, sealed = _sealed()
    other_nonce = bytes((b ^ 0xFF) for b in NONCE)
    assert aead.open(ENC_KEY, MAC_KEY, other_nonce, AAD, sealed) is None


def test_overhead_is_exactly_the_four_byte_tag():
    # The airtime case for secure mode rests on this: CTR adds no padding, so a sealed frame
    # is the plaintext plus only the 4-byte tag.
    aead, sealed = _sealed()
    assert aead.TAG_LEN == 4
    assert len(sealed) == len(PLAINTEXT) + 4


def test_empty_plaintext_seals_to_just_the_tag():
    aead = detect_aead()
    sealed = aead.seal(ENC_KEY, MAC_KEY, NONCE, AAD, b"")
    assert len(sealed) == 4
    assert aead.open(ENC_KEY, MAC_KEY, NONCE, AAD, sealed) == b""


def test_detect_aead_returns_a_self_tested_working_backend():
    # On any platform where detect_aead returns non-None, the backend must actually work.
    aead = detect_aead()
    assert aead is not None            # CPython has the cryptography backend
    assert _self_test(aead) is True


def test_self_test_rejects_a_backend_whose_ctr_throws():
    # Mirrors ucryptolib present but without the CTR build flag: import "succeeds", first use
    # throws. _self_test must catch it so detect_aead can degrade to open instead of crashing.
    def broken_ctr(key, nonce, data):
        raise ValueError("AES-CTR not compiled in")
    assert _self_test(Ctr_hmac_aead(broken_ctr)) is False


def test_self_test_rejects_a_backend_that_corrupts():
    # A backend that returns wrong bytes (not just throws) also fails the round-trip.
    def bad_ctr(key, nonce, data):
        return bytes(len(data))        # zeros — round-trip won't recover the plaintext
    assert _self_test(Ctr_hmac_aead(bad_ctr)) is False

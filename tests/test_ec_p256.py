"""Unit — pure-Python P-256 ECDH (the handshake's key agreement).

A minimal, dependency-free P-256 so the ephemeral-static ECDH handshake runs on-device
without a native crypto module. The one property that matters: two parties who exchange
public keys and each multiply by their own private scalar arrive at the *same* shared
secret, which the KDF then turns into the session keys. Public keys are validated to be
real points on the curve (a point off the curve is a classic small-subgroup / invalid-key
attack vector).
"""
import os

from AlLoRa.Security.ec_p256 import (
    generate_private_key, public_key_uncompressed, ecdh_shared_secret,
    is_on_curve, decode_public_key,
)


def test_two_parties_derive_the_same_shared_secret():
    a_priv = generate_private_key(os.urandom)
    b_priv = generate_private_key(os.urandom)
    a_pub = public_key_uncompressed(a_priv)
    b_pub = public_key_uncompressed(b_priv)

    a_shared = ecdh_shared_secret(a_priv, b_pub)   # a's private, b's public
    b_shared = ecdh_shared_secret(b_priv, a_pub)   # b's private, a's public

    assert a_shared == b_shared
    assert len(a_shared) == 32


def test_a_generated_public_key_is_on_the_curve():
    pub = public_key_uncompressed(generate_private_key(os.urandom))
    assert pub[0] == 0x04 and len(pub) == 65
    assert is_on_curve(decode_public_key(pub))


def test_a_point_off_the_curve_is_rejected():
    import pytest
    # A well-formed 65-byte key whose (x, y) is not a curve point (invalid-key attack).
    forged = b"\x04" + (1).to_bytes(32, "big") + (1).to_bytes(32, "big")
    with pytest.raises(ValueError):
        decode_public_key(forged)


def test_a_malformed_public_key_is_rejected():
    import pytest
    with pytest.raises(ValueError):
        decode_public_key(b"\x04" + b"\x00" * 10)      # too short
    with pytest.raises(ValueError):
        decode_public_key(b"\x02" + b"\x00" * 64)      # not the uncompressed 0x04 prefix

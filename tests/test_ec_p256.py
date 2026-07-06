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
    is_on_curve, decode_public_key, scalar_mult, point_add, G, N, INF,
)

# NIST P-256 known small multiples of the base point G (widely published test vectors).
_KNOWN_MULTIPLES = {
    1: (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
        0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5),
    2: (0x7CF27B188D034F7E8A52380304B51AC3C08969E277F21B35A60B48FC47669978,
        0x07775510DB8ED040293D9AC69F7430DBBA7DADE63CE982299E04B79D227873D1),
    3: (0x5ECBE4D1A6330A44C8F7EF951D4BF165E6C6B721EFADA985FB41661BC6E7FD6C,
        0x8734640C4998FF7E374B06CE1A64A2ECD82AB036384FB83D9A79B127A27D5032),
}


def test_scalar_mult_matches_nist_known_answer_vectors():
    # Absolute correctness anchor: k*G against published P-256 values. A Jacobian-formula
    # transcription bug that still "agrees with itself" would pass the ECDH test below but
    # fail here.
    for k, expected in _KNOWN_MULTIPLES.items():
        assert scalar_mult(k, G) == expected


def test_scalar_mult_matches_an_independent_affine_ladder():
    # Cross-check scalar_mult against a plain affine double-and-add built from the module's
    # affine point_add — an independent code path must agree for random scalars.
    def affine_mul(k, pt):
        r, addend = INF, pt
        while k:
            if k & 1:
                r = point_add(r, addend)
            addend = point_add(addend, addend)
            k >>= 1
        return r

    for _ in range(5):
        k = (int.from_bytes(os.urandom(32), "big") % (N - 1)) + 1
        assert scalar_mult(k, G) == affine_mul(k, G)


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

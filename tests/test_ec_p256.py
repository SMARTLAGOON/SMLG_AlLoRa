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
    is_on_curve, decode_public_key, scalar_mult, point_add, ecdsa_verify,
    ecdsa_sign, G, N, INF,
)

# Canonical P-256 / SHA-256 ECDSA vectors from RFC 6979 Appendix A.2.5 (the deterministic-k
# signatures for the key x=C9AF..6721 over messages "sample" and "test"). Frozen as literals so
# the suite stays dependency-free; each was cross-checked to match RFC 6979's published (r, s)
# with an independent library at authoring time. These are the source of truth ecdsa_verify is
# measured against, so a bug in our verify math disagrees with them rather than agreeing with
# itself. Format: pubkey = SEC1 uncompressed (65 B), digest = SHA-256(msg) (32 B), sig = r||s (64 B).
_RFC6979_PUBKEY = bytes.fromhex(
    "0460fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6"
    "7903fe1008b8bc99a41ae9e95628bc64f2f1b20c2d7e9f5177a3c294d4462299"
)
_SAMPLE_DIGEST = bytes.fromhex("af2bdbe1aa9b6ec1e2ade1d694f41fc71a831d0268e9891562113d8a62add1bf")
_SAMPLE_SIG = bytes.fromhex(
    "efd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716"
    "f7cb1c942d657c41d436c7a1b6e29f65f3e900dbb9aff4064dc4ab2f843acda8"
)
_RFC6979_PRIV = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
_TEST_DIGEST = bytes.fromhex("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")
_TEST_SIG = bytes.fromhex(
    "f1abb023518351cd71d881567b1ea663ed3efcf6c5132b354f28d3b0b7d38367"
    "019f4113742a2b14bd25926b49c649155f267e60d3814b4c0cc84250e46f0083"
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


def test_ecdsa_verify_accepts_a_genuine_rfc6979_signature():
    # A real P-256/SHA-256 signature over the control-root pubkey must verify. This is the
    # authenticity guarantee the whole downlink-command feature rests on.
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, _SAMPLE_SIG) is True
    assert ecdsa_verify(_RFC6979_PUBKEY, _TEST_DIGEST, _TEST_SIG) is True


def test_ecdsa_verify_rejects_a_tampered_message():
    # Flipping one bit of the signed digest (a forged control command reusing a captured
    # signature) must fail. This is the whole point: the signature binds these exact bytes.
    tampered = bytearray(_SAMPLE_DIGEST)
    tampered[0] ^= 0x01
    assert ecdsa_verify(_RFC6979_PUBKEY, bytes(tampered), _SAMPLE_SIG) is False


def test_ecdsa_verify_rejects_a_tampered_signature():
    # A single-bit change in r, and separately in s, must both fail.
    r_flip = bytearray(_SAMPLE_SIG); r_flip[0] ^= 0x01
    s_flip = bytearray(_SAMPLE_SIG); s_flip[32] ^= 0x01
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, bytes(r_flip)) is False
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, bytes(s_flip)) is False


def test_ecdsa_verify_rejects_a_signature_from_a_different_key():
    # A genuine signature verified against the wrong (attacker's) public key must fail: an
    # unregistered signer cannot pass off a valid-looking command as the control-root's.
    other_pub = public_key_uncompressed(generate_private_key(os.urandom))
    assert ecdsa_verify(other_pub, _SAMPLE_DIGEST, _SAMPLE_SIG) is False


def test_ecdsa_verify_rejects_structurally_invalid_signatures():
    # Wrong length, and r or s outside [1, N): rejected without raising (an unverifiable
    # signature is simply not authentic).
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, _SAMPLE_SIG[:-1]) is False   # 63 bytes
    zero_r = (0).to_bytes(32, "big") + _SAMPLE_SIG[32:]
    zero_s = _SAMPLE_SIG[:32] + (0).to_bytes(32, "big")
    r_eq_n = N.to_bytes(32, "big") + _SAMPLE_SIG[32:]
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, zero_r) is False
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, zero_s) is False
    assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, r_eq_n) is False


# --- ECDSA signing: the minting half of the control root ------------------------------
#
# The control root is an authority a node verifies against, so something must be able to
# mint what it verifies. Signing uses the RFC 6979 deterministic nonce rather than a random
# one: a nonce that repeats or is biased leaks the private key outright from two signatures,
# and on an ESP32 `urandom` is only cryptographically strong with the RF subsystem enabled,
# which a USB-attached minting Hub need not have. Deriving the nonce from the key and the
# digest removes the RNG, and with it the whole failure mode.
#
# It also makes the vectors above an exact test of signing rather than a round trip: RFC
# 6979's published (r, s) is what a correct deterministic signer must produce byte for byte,
# so a wrong-but-self-consistent implementation disagrees with the RFC instead of agreeing
# with itself.


def test_the_rfc6979_private_key_matches_the_pinned_public_key():
    # Ties the private scalar to the public key the verify tests already trust, so the
    # signing vectors below rest on the same published pair and not on a fresh assumption.
    assert public_key_uncompressed(_RFC6979_PRIV) == _RFC6979_PUBKEY


def test_ecdsa_sign_reproduces_the_published_rfc6979_signatures():
    # The exactness test. Both A.2.5 vectors, byte for byte.
    assert ecdsa_sign(_RFC6979_PRIV, _SAMPLE_DIGEST) == _SAMPLE_SIG
    assert ecdsa_sign(_RFC6979_PRIV, _TEST_DIGEST) == _TEST_SIG


def test_a_freshly_minted_key_signs_something_its_public_half_verifies():
    # The actual minting use: a control root generated on a Hub signs an artifact digest, and
    # a node holding only the public half accepts it.
    priv = generate_private_key(os.urandom)
    pub = public_key_uncompressed(priv)
    digest = bytes.fromhex("9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08")

    signature = ecdsa_sign(priv, digest)

    assert len(signature) == 64
    assert ecdsa_verify(pub, digest, signature) is True


def test_a_signature_does_not_carry_over_to_another_digest():
    # The binding that makes a signed control artifact worth signing: a signature minted over
    # one payload must not authenticate a different one.
    priv = generate_private_key(os.urandom)
    pub = public_key_uncompressed(priv)

    signature = ecdsa_sign(priv, _SAMPLE_DIGEST)

    assert ecdsa_verify(pub, _TEST_DIGEST, signature) is False


def test_signing_the_same_digest_twice_returns_the_same_bytes():
    # Determinism is the point of the RFC 6979 nonce, not an accident to be tidied away: it is
    # what removes the RNG whose failure would leak the private scalar. Pinned so that adding
    # randomness back here fails loudly rather than quietly.
    assert ecdsa_sign(_RFC6979_PRIV, _SAMPLE_DIGEST) == ecdsa_sign(_RFC6979_PRIV, _SAMPLE_DIGEST)


def test_signing_refuses_a_key_or_digest_it_cannot_use():
    # A private scalar outside [1, N) is a provisioning error, and a digest that is not
    # SHA-256-sized means the caller hashed with something else. Either would otherwise mint a
    # signature that no verifier accepts, which reads as a broken link rather than a bad key.
    import pytest
    with pytest.raises(ValueError):
        ecdsa_sign(0, _SAMPLE_DIGEST)
    with pytest.raises(ValueError):
        ecdsa_sign(N, _SAMPLE_DIGEST)
    with pytest.raises(ValueError):
        ecdsa_sign(_RFC6979_PRIV, _SAMPLE_DIGEST[:31])

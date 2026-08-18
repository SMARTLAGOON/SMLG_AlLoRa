"""Unit — pure-Python P-256 ECDH (the handshake's key agreement).

A minimal, dependency-free P-256 so the ephemeral-static ECDH handshake runs on-device
without a native crypto module. The one property that matters: two parties who exchange
public keys and each multiply by their own private scalar arrive at the *same* shared
secret, which the KDF then turns into the session keys. Public keys are validated to be
real points on the curve (a point off the curve is a classic small-subgroup / invalid-key
attack vector).
"""
import os

from AlLoRa.Security import ec_p256
from AlLoRa.Security.ec_p256 import (
    generate_private_key, public_key_uncompressed, ecdh_shared_secret,
    is_on_curve, decode_public_key, scalar_mult, point_add, ecdsa_verify,
    ecdsa_sign, G, N, P, INF,
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


def _affine_mul(k, pt):
    """A plain affine double-and-add built from the module's affine point_add. Deliberately
    the slowest, most obvious implementation available: it shares no code with the Jacobian
    windowed ladder, so agreement between the two is real evidence rather than a formula
    agreeing with itself."""
    r, addend = INF, pt
    while k:
        if k & 1:
            r = point_add(r, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return r


def test_scalar_mult_matches_an_independent_affine_ladder():
    # Cross-check scalar_mult against a plain affine double-and-add built from the module's
    # affine point_add — an independent code path must agree for random scalars.
    for _ in range(5):
        k = (int.from_bytes(os.urandom(32), "big") % (N - 1)) + 1
        assert scalar_mult(k, G) == _affine_mul(k, G)


def test_scalar_mult_agrees_with_the_affine_ladder_on_awkward_scalars():
    # A windowed ladder reads the scalar in fixed-width digits, so the scalars that break it
    # are the ones with structure at a digit boundary: an all-zero digit to skip, an all-ones
    # digit at the top of the table, the very top and bottom of the range. Random 256-bit
    # scalars almost never contain any of these, which is exactly why they are listed here.
    awkward = [
        1, 2, 3, 15, 16, 17, 255, 256,
        N - 1,                                   # the top of the group
        0x0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F,
        0xF0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0F0 % N,
        0xFFFFFFFF0000000000000000000000000000000000000000000000000000FFFF % N,
        (1 << 255) % N,                          # a single high bit
        0x1000000000000000000000000000000000000000000000000000000000000001 % N,
    ]
    for k in awkward:
        assert scalar_mult(k, G) == _affine_mul(k, G), "k = {}".format(hex(k))


def test_scalar_mult_reduces_the_scalar_modulo_the_group_order():
    # P-256 has cofactor 1, so every curve point has order N and k*Pt depends only on k mod N.
    # A ladder that reads a fixed 256 bits of the scalar must therefore reduce first, or a
    # scalar at or above N walks off the end of what it reads.
    assert scalar_mult(N, G) is INF
    assert scalar_mult(2 * N, G) is INF
    assert scalar_mult(N + 5, G) == scalar_mult(5, G)
    assert scalar_mult(N + 5, G) == _affine_mul(5, G)


def test_scalar_mult_negates_the_point_for_a_negative_scalar():
    # (-k)*Pt is the reflection of k*Pt in the x-axis. Pinned because it is the one caller
    # behaviour that is not exercised by the handshake or by verify, so a ladder rewrite could
    # drop it silently.
    k = (int.from_bytes(os.urandom(32), "big") % (N - 1)) + 1
    x, y = scalar_mult(k, G)
    assert scalar_mult(-k, G) == (x, (-y) % P)
    assert scalar_mult(-1, G) == (G[0], (-G[1]) % P)


def test_scalar_mult_returns_infinity_for_the_identity_point():
    assert scalar_mult(7, INF) is INF
    assert scalar_mult(0, G) is INF


def test_every_scalar_multiple_lands_back_on_the_curve():
    # The cheapest possible check on a formula transcription: a wrong Jacobian doubling or
    # addition almost always produces a point that is not on P-256 at all.
    for _ in range(3):
        k = (int.from_bytes(os.urandom(32), "big") % (N - 1)) + 1
        assert is_on_curve(scalar_mult(k, G))


# --- The Jacobian point operations, and how many of them a call is allowed to cost ---------
#
# These reach past the public surface on purpose. The curve formulas are specialised for
# P-256 (a = -3), so a transcription slip in one of them is a security bug that the public
# ECDH test would still pass, because both sides would agree on the same wrong answer.


def _to_jacobian(pt):
    return (pt[0], pt[1], 1)


def test_jacobian_doubling_matches_affine_doubling():
    # The doubling formula is written for a = -3 rather than for a general a. Checked here
    # against the module's own affine point_add, which uses the generic lambda and A directly.
    for k in (1, 2, 3, 7, 12345):
        pt = scalar_mult(k, G)
        doubled = ec_p256._jac_to_affine(ec_p256._jac_double(_to_jacobian(pt)))
        assert doubled == point_add(pt, pt)


def test_jacobian_doubling_preserves_the_identity():
    # Two ways to reach the point at infinity: a Z of zero (the accumulator before the ladder
    # takes its first bit), and a Y of zero (a point of order two, which P-256 has none of but
    # the formula must still not mangle). Both must double to something with Z = 0.
    assert ec_p256._jac_double(ec_p256._JAC_INF)[2] == 0
    assert ec_p256._jac_double((5, 0, 1))[2] == 0


def test_jacobian_addition_matches_affine_addition():
    a, b = scalar_mult(3, G), scalar_mult(11, G)
    summed = ec_p256._jac_to_affine(ec_p256._jac_add(_to_jacobian(a), _to_jacobian(b)))
    assert summed == point_add(a, b)


def test_jacobian_addition_handles_the_doubling_and_inverse_cases():
    # Adding a point to itself has to fall through to the doubling formula (the general
    # addition divides by zero there), and adding a point to its own reflection is the
    # identity. Both are reachable from a windowed ladder whose accumulator happens to match a
    # table entry.
    a = scalar_mult(9, G)
    same = ec_p256._jac_to_affine(ec_p256._jac_add(_to_jacobian(a), _to_jacobian(a)))
    assert same == point_add(a, a)

    negated = (a[0], (-a[1]) % P)
    assert ec_p256._jac_add(_to_jacobian(a), _to_jacobian(negated))[2] == 0


class _PointOpCounter:
    """Count the Jacobian point operations one call performs.

    Not a speed test. On the ESP32 a single verify allocates about 2 MB of short-lived
    256-bit integers, so the collector runs on the order of a hundred times inside it and
    each run walks everything the node is holding alive. The measured cost is therefore
    proportional to the number of field operations, and those are proportional to the point
    operations counted here. Bounding the count is the only way to pin that property from
    CPython, where the collector behaves nothing like the board's.
    """

    def __enter__(self):
        self.doubles = 0
        self.adds = 0
        self._real_double = ec_p256._jac_double
        self._real_add = ec_p256._jac_add

        def counting_double(pt):
            self.doubles += 1
            return self._real_double(pt)

        def counting_add(p1, p2):
            self.adds += 1
            return self._real_add(p1, p2)

        ec_p256._jac_double = counting_double
        ec_p256._jac_add = counting_add
        return self

    def __exit__(self, *exc):
        ec_p256._jac_double = self._real_double
        ec_p256._jac_add = self._real_add
        return False


def test_a_scalar_multiplication_stays_inside_its_point_operation_budget():
    # A naive bit-at-a-time ladder needs one addition per set bit, about 128 for a random
    # 256-bit scalar. Reading the scalar four bits at a time cuts that to at most one per
    # digit, 64, plus the table it builds once. The doubling count does not change; the
    # additions are where the churn was.
    k = (int.from_bytes(os.urandom(32), "big") % (N - 1)) + 1
    with _PointOpCounter() as count:
        scalar_mult(k, G)
    assert count.adds <= 80, "additions per scalar_mult: {}".format(count.adds)
    assert count.doubles <= 275, "doublings per scalar_mult: {}".format(count.doubles)


def test_a_signature_verification_costs_one_ladder_and_not_two():
    # Verify computes u1*G + u2*Q. Running two separate ladders doubles the work; running one
    # ladder over both scalars at once shares every doubling between them. This is the single
    # largest saving available in the module, and the number it moves is published, so it is
    # pinned rather than left to be re-discovered.
    with _PointOpCounter() as count:
        assert ecdsa_verify(_RFC6979_PUBKEY, _SAMPLE_DIGEST, _SAMPLE_SIG) is True
    assert count.doubles <= 275, "doublings per verify: {}".format(count.doubles)
    assert count.adds <= 150, "additions per verify: {}".format(count.adds)


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


def test_the_deterministic_nonce_does_not_go_through_the_hmac_module(monkeypatch):
    # The signing path builds its RFC 6979 nonce from HMAC-SHA256, and it used to get that from
    # MicroPython's pure-Python `hmac`, which costs 19.4 ms a call on the Edge against 1.1 ms for
    # the package's own primitive. RFC 6979 makes several calls per signature and every one of
    # them lands inside the handshake.
    #
    # Asserting the dependency is gone is the only check CI can make that would fail if the
    # wrapper came back: on CPython `hmac` is a C extension and fast, so the published-vector
    # test above would stay green either way. Same reasoning, and same shape, as the guard in
    # tests/test_hmac_sha256.py.
    import hmac

    def _unavailable(*args, **kwargs):
        raise AssertionError("ecdsa_sign must not build its nonce through the hmac module")

    monkeypatch.setattr(hmac, "new", _unavailable)
    assert ecdsa_sign(_RFC6979_PRIV, _SAMPLE_DIGEST) == _SAMPLE_SIG


def test_the_nonce_hmac_still_agrees_with_the_standard_library():
    # The vectors above already prove byte-identity end to end, but they exercise one key and two
    # digests. This pins the primitive itself across the shapes RFC 6979 actually feeds it: a
    # 32-byte key with 97-byte data on the first pass, and a 32-byte key with 33 on later ones.
    import hashlib
    import hmac

    for klen in (1, 32, 64, 65, 100):
        for dlen in (0, 1, 32, 33, 97, 200):
            key = bytes((i * 7 + klen) % 256 for i in range(klen))
            data = bytes((i * 3 + dlen) % 256 for i in range(dlen))
            assert ec_p256._hmac_sha256(key, data) == \
                hmac.new(key, data, hashlib.sha256).digest(), \
                "nonce HMAC disagrees at key={} data={}".format(klen, dlen)

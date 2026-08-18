"""Minimal pure-Python P-256 (secp256r1) for the ECDH handshake and the control root.

Elliptic-curve math with no native crypto module, so ephemeral-static ECDH runs on the
AlLoRa firmware. The asymmetric cost is seconds of scalar multiplication on-device, and it is
paid once per session at the handshake, never per frame. Consolidated from the project's
SecureAlLoRa reference implementation (the two hand-rolled P-256 files merged into one),
carrying what ECDH needs (keypair generation, SEC1 uncompressed points, on-curve validation, the
shared-secret computation) plus both halves of the control-root signature: verification, run
by a field node on the rare downlink control artifact (config/OTA/model) and never on the
per-frame hot path, and signing, run by whoever holds the root private key.

The curve math is dependency-free; signing additionally needs HMAC + ``hashlib`` for its
deterministic nonce, which the security layer already requires (the KDF and the AEAD are
built on both, and secure mode refuses to start without them). The HMAC comes from this
package's own ``hmac_sha256`` rather than from the ``hmac`` module: MicroPython's is pure
Python and costs 19.4 ms a call on the Edge against 1.1 ms, and RFC 6979 makes several calls
per signature, all of them inside the handshake.

Public keys are always validated to be real points on the curve before use. Accepting an
off-curve point is a classic invalid-key attack that can leak the private scalar.
"""
import hashlib

from AlLoRa.Security.hmac_sha256 import hmac_sha256

# secp256r1 domain parameters.
P  = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A  = (P - 3) % P
B  = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N  = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
G  = (GX, GY)

INF = None  # the point at infinity (identity)


def inv_mod(k, m=P):
    if k == 0:
        raise ZeroDivisionError("inverse of 0 does not exist")
    return pow(k, m - 2, m)


def is_on_curve(point):
    if point is INF:
        return True
    x, y = point
    return (y * y - (x * x * x + A * x + B)) % P == 0


def point_add(p1, p2):
    # The plain affine addition, kept as the readable reference the Jacobian formulas below
    # are checked against. The hot paths do not call it.
    if p1 is INF:
        return p2
    if p2 is INF:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return INF
    if p1 == p2:
        if y1 == 0:
            return INF
        lam = ((3 * x1 * x1 + A) * inv_mod(2 * y1)) % P
    else:
        lam = ((y2 - y1) * inv_mod((x2 - x1) % P)) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


# --- Jacobian projective coordinates: the fast path for scalar_mult --------------------------
# scalar_mult runs several times per handshake. In affine coordinates every point add/double
# needs a modular inverse (inv_mod -> a 256-bit Fermat exponentiation), ~384 of them per scalar
# multiplication, about 13 s on the ESP32, which overruns the handshake's receive window and
# blocks the radio loop. Jacobian coordinates, where affine (x, y) = (X/Z^2, Y/Z^3), let the
# whole double-and-add ladder run with NO inverses; a single inverse converts the result back to
# affine at the very end. The curve points are identical, so public keys and shared secrets are
# byte-for-byte unchanged (pinned by the NIST known-answer test). Only the speed differs.
#
# The second thing these formulas are written for is allocation. Python integers are immutable,
# so every modular multiply allocates a fresh object, and one scalar multiplication used to
# allocate over 1 MB of them. On the ESP32 that is more than the working heap, so the collector
# runs on the order of a hundred times inside a single call and each run walks everything the
# node holds alive: the cost of the curve math is proportional to how much memory the node is
# using elsewhere. Hence the intermediate reductions that are skipped below wherever the operand
# stays small enough to multiply safely, and the digit-at-a-time ladders that follow.
#
# Formulas: EFD "dbl-2001-b" (specialised for a = -3, which is what A is on this curve) and
# "add-2007-bl".

_JAC_INF = (1, 1, 0)   # the identity has Z = 0


def _jac_double(pt):
    # a = -3 on P-256 (A is defined above as P - 3), so 3*X^2 + a*Z^4 factors as
    # 3*(X - Z^2)*(X + Z^2). That saves one field multiplication and, more to the point, four
    # of the temporaries the general formula needs: 27 intermediate integers per call against
    # 38. A curve with any other a would need the general form back.
    X1, Y1, Z1 = pt
    if Z1 == 0 or Y1 == 0:
        return _JAC_INF
    delta = (Z1 * Z1) % P
    gamma = (Y1 * Y1) % P
    beta = (X1 * gamma) % P
    alpha = (3 * (X1 - delta) * (X1 + delta)) % P
    X3 = (alpha * alpha - 8 * beta) % P
    u = Y1 + Z1
    Z3 = (u * u - gamma - delta) % P
    Y3 = (alpha * (4 * beta - X3) - 8 * gamma * gamma) % P
    return (X3, Y3, Z3)


def _jac_add(p1, p2):
    X1, Y1, Z1 = p1
    X2, Y2, Z2 = p2
    if Z1 == 0:
        return p2
    if Z2 == 0:
        return p1
    Z1Z1 = (Z1 * Z1) % P
    Z2Z2 = (Z2 * Z2) % P
    U1 = (X1 * Z2Z2) % P
    U2 = (X2 * Z1Z1) % P
    S1 = (Y1 * Z2 * Z2Z2) % P
    S2 = (Y2 * Z1 * Z1Z1) % P
    if U1 == U2:
        if S1 != S2:
            return _JAC_INF          # p1 + (-p1) = identity
        return _jac_double(p1)       # p1 == p2
    # H and r are left unreduced: both stay under 2P, so every product they enter is still
    # small enough that the single reduction at the end of each line is the only one needed.
    H = U2 - U1
    I = (4 * H * H) % P
    J = (H * I) % P
    r = 2 * (S2 - S1)
    V = (U1 * I) % P
    X3 = (r * r - J - 2 * V) % P
    Y3 = (r * (V - X3) - 2 * S1 * J) % P
    zsum = Z1 + Z2
    Z3 = (((zsum * zsum - Z1Z1 - Z2Z2) % P) * H) % P
    return (X3, Y3, Z3)


def _jac_to_affine(pt):
    X, Y, Z = pt
    if Z == 0:
        return INF
    zi = inv_mod(Z)
    zi2 = (zi * zi) % P
    zi3 = (zi2 * zi) % P
    return ((X * zi2) % P, (Y * zi3) % P)


def _jacobian(point):
    """An affine curve point as a Jacobian triple with Z = 1."""
    return (point[0] % P, point[1] % P, 1)


def scalar_mult(k, point):
    if point is INF:
        return INF
    # P-256 has cofactor 1, so every point on it has order N and k*point depends only on
    # k mod N. Reducing here is what lets the ladder read a fixed 256 bits of scalar, and it
    # folds a negative scalar onto the equivalent positive one on the way.
    k %= N
    if k == 0:
        return INF

    # One table of small multiples, built once, so the ladder spends at most one addition per
    # four bits of scalar rather than one per set bit: about 64 additions instead of 128. Even
    # multiples come from the cheaper doubling.
    table = [_JAC_INF, _jacobian(point)]
    for i in range(2, 16):
        if i & 1:
            table.append(_jac_add(table[i - 1], table[1]))
        else:
            table.append(_jac_double(table[i >> 1]))

    # Left to right, four bits at a time. Reading the scalar as bytes rather than shifting it
    # keeps every digit a small integer and avoids int.bit_length(), which MicroPython does
    # not have: 32 bytes is always enough for a scalar reduced mod N, and the leading zero
    # digits cost only the identity check at the top of _jac_double.
    R = _JAC_INF
    for byte in k.to_bytes(32, "big"):
        R = _jac_double(_jac_double(_jac_double(_jac_double(R))))
        high = byte >> 4
        if high:
            R = _jac_add(R, table[high])
        R = _jac_double(_jac_double(_jac_double(_jac_double(R))))
        low = byte & 0x0F
        if low:
            R = _jac_add(R, table[low])
    return _jac_to_affine(R)


def _joint_scalar_mult(k1, point1, k2, point2):
    """Return ``k1*point1 + k2*point2`` as an affine point, from a single ladder.

    Two separate scalar multiplications would double their own accumulator 256 times each.
    Stepping both scalars through one accumulator, two bits at a time, shares every one of
    those doublings, so the pair costs about what one used to. Both points must be real
    affine curve points, not the identity; the only caller is ecdsa_verify, where they are
    the base point and a validated public key.
    """
    k1 %= N
    k2 %= N
    # table[4*i + j] = i*point1 + j*point2, for i and j in 0..3.
    m1 = [_JAC_INF, _jacobian(point1)]
    m2 = [_JAC_INF, _jacobian(point2)]
    for m in (m1, m2):
        m.append(_jac_double(m[1]))
        m.append(_jac_add(m[2], m[1]))
    table = [_JAC_INF] * 16
    for i in range(4):
        for j in range(4):
            if i and j:
                table[4 * i + j] = _jac_add(m1[i], m2[j])
            elif i:
                table[4 * i + j] = m1[i]
            elif j:
                table[4 * i + j] = m2[j]

    R = _JAC_INF
    b1 = k1.to_bytes(32, "big")
    b2 = k2.to_bytes(32, "big")
    for i in range(32):
        d1 = b1[i]
        d2 = b2[i]
        for shift in (6, 4, 2, 0):
            R = _jac_double(_jac_double(R))
            d = (((d1 >> shift) & 3) << 2) | ((d2 >> shift) & 3)
            if d:
                R = _jac_add(R, table[d])
    return _jac_to_affine(R)


def generate_private_key(randfunc):
    """Return a uniform private scalar in [1, N). ``randfunc(nbytes)`` supplies entropy
    (e.g. ``os.urandom``), injected so the caller controls the RNG."""
    while True:
        d = int.from_bytes(randfunc(32), "big") % N
        if 1 <= d < N:
            return d


def public_key_uncompressed(priv_d):
    """Return the SEC1 uncompressed public key ``0x04 || X(32) || Y(32)`` for a private
    scalar."""
    x, y = scalar_mult(priv_d, G)
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def decode_public_key(pub_bytes):
    """Parse and validate a SEC1 uncompressed public key into a point, raising ValueError
    on a bad format or a point that is not on the curve."""
    if len(pub_bytes) != 65 or pub_bytes[0] != 0x04:
        raise ValueError("expected a 65-byte uncompressed public key (0x04 || X || Y)")
    x = int.from_bytes(pub_bytes[1:33], "big")
    y = int.from_bytes(pub_bytes[33:65], "big")
    point = (x, y)
    if not is_on_curve(point):
        raise ValueError("public key is not on the P-256 curve")
    return point


def ecdh_shared_secret(priv_d, peer_pub_uncompressed):
    """ECDH: the X coordinate of ``priv_d * peer_pub``, as 32 big-endian bytes. Validates
    the peer key and rejects a degenerate result."""
    peer_point = decode_public_key(peer_pub_uncompressed)
    shared = scalar_mult(priv_d, peer_point)
    if shared is INF:
        raise ValueError("degenerate shared secret")
    x, _ = shared
    return x.to_bytes(32, "big")


_N_BITS = 256  # bit length of the group order N (fixed for P-256); int.bit_length() is absent on MicroPython.


def _bits_to_int(digest):
    """FIPS 186-4 bits2int: the leftmost ``_N_BITS`` bits of the message digest as an integer.
    For a 32-byte SHA-256 digest this is the whole digest (256 bits, no truncation); a longer
    hash is shifted right so only the top _N_BITS bits are used."""
    z = int.from_bytes(digest, "big")
    excess = 8 * len(digest) - _N_BITS
    if excess > 0:
        z >>= excess
    return z


def ecdsa_verify(public_key, digest, signature):
    """Verify a P-256/SHA-256 ECDSA signature. Returns ``True`` only for a genuine signature.

    ``public_key`` is a SEC1 uncompressed key (``0x04 || X || Y``, 65 B) whose authority the
    caller has already established (the pinned control-root); a malformed or off-curve key is a
    provisioning error and raises ``ValueError`` via ``decode_public_key``. ``digest`` is the
    SHA-256 hash of the signed message; ``signature`` is the raw ``r || s`` pair (64 B). A
    structurally invalid signature (wrong length, ``r`` or ``s`` outside ``[1, N)``) returns
    ``False`` rather than raising: an unverifiable signature is simply not authentic, not an
    error to handle. This runs only on the rare downlink control artifact, never per frame."""
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    Q = decode_public_key(public_key)
    z = _bits_to_int(digest)
    w = inv_mod(s, N)
    u1 = (z * w) % N
    u2 = (r * w) % N
    point = _joint_scalar_mult(u1, G, u2, Q)
    if point is INF:
        return False
    return point[0] % N == r


def _hmac_sha256(key, data):
    # Same bytes as hmac.new(key, data, sha256).digest(), which is what RFC 6979 requires and
    # what the vectors pin; the difference is only that this one is not the pure-Python module.
    return hmac_sha256(bytes(key), bytes(data))


def ecdsa_sign(priv_d, digest):
    """Sign a SHA-256 ``digest`` with the P-256 private scalar ``priv_d``, returning the raw
    ``r || s`` pair (64 B) that ``ecdsa_verify`` checks.

    The per-signature nonce is derived from the key and the digest (RFC 6979 section 3.2)
    rather than drawn from an RNG. ECDSA leaks the private scalar outright if a nonce ever
    repeats or is measurably biased, and ``urandom`` on an ESP32 is only cryptographically
    strong while the RF subsystem is enabled, which a minting node need not have on. Deriving
    the nonce removes the RNG, and the whole failure mode with it. A visible consequence:
    signing the same digest twice returns the same bytes, which is expected here and is what
    lets the published vectors pin this code exactly.

    This is the minting side of the control root, run by whoever holds the root private key.
    A field node only ever verifies.
    """
    if not (1 <= priv_d < N):
        raise ValueError("private scalar out of range [1, N)")
    if len(digest) != 32:
        raise ValueError("expected a 32-byte SHA-256 digest")

    # Seed the HMAC-SHA256 generator with the key and the digest, so the nonce is a function
    # of exactly what is being signed and by whom.
    key_octets = priv_d.to_bytes(32, "big")                          # int2octets(x)
    digest_octets = (_bits_to_int(digest) % N).to_bytes(32, "big")   # bits2octets(h1)
    gen_key = b"\x00" * 32
    gen_val = b"\x01" * 32
    gen_key = _hmac_sha256(gen_key, gen_val + b"\x00" + key_octets + digest_octets)
    gen_val = _hmac_sha256(gen_key, gen_val)
    gen_key = _hmac_sha256(gen_key, gen_val + b"\x01" + key_octets + digest_octets)
    gen_val = _hmac_sha256(gen_key, gen_val)

    z = _bits_to_int(digest)
    while True:
        # One HMAC output is exactly the 256 bits the group order needs, so a candidate is
        # one step of the generator.
        gen_val = _hmac_sha256(gen_key, gen_val)
        k = _bits_to_int(gen_val)
        if 1 <= k < N:
            point = scalar_mult(k, G)
            if point is not INF:
                r = point[0] % N
                if r != 0:
                    s = (inv_mod(k, N) * (z + r * priv_d)) % N
                    if s != 0:
                        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        # An unusable candidate (out of range, or a degenerate r or s, all vanishingly rare):
        # re-key the generator and draw a fresh one. Nudging k instead would bias it, and a
        # biased nonce is the same key-recovery hazard a random one would have been.
        gen_key = _hmac_sha256(gen_key, gen_val + b"\x00")
        gen_val = _hmac_sha256(gen_key, gen_val)

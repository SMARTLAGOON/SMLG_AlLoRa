"""Minimal pure-Python P-256 (secp256r1) for the ECDH handshake.

Dependency-free elliptic-curve math so ephemeral-static ECDH runs on the AlLoRa firmware
with no native crypto module. The asymmetric cost is paid once per session (a few hundred
ms of scalar multiplication on-device), never per frame. Consolidated from the project's
SecureAlLoRa reference implementation (the two hand-rolled P-256 files merged into one),
carrying what ECDH needs (keypair generation, SEC1 uncompressed points, on-curve validation,
the shared-secret computation) plus ECDSA verification for the control-root downlink. Verify
runs only on the rare downlink control artifact (config/OTA/model), never on the per-frame
hot path; there is no signing here, since the field node only ever verifies.

Public keys are always validated to be real points on the curve before use. Accepting an
off-curve point is a classic invalid-key attack that can leak the private scalar.
"""

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
# Formulas: EFD "dbl-2007-bl" / "add-2007-bl" (general a; here a = A).

_JAC_INF = (1, 1, 0)   # the identity has Z = 0


def _jac_double(pt):
    X1, Y1, Z1 = pt
    if Z1 == 0 or Y1 == 0:
        return _JAC_INF
    XX = (X1 * X1) % P
    YY = (Y1 * Y1) % P
    YYYY = (YY * YY) % P
    ZZ = (Z1 * Z1) % P
    t = (X1 + YY) % P
    S = (2 * ((t * t - XX - YYYY) % P)) % P
    M = (3 * XX + A * ((ZZ * ZZ) % P)) % P
    T = (M * M - 2 * S) % P
    u = (Y1 + Z1) % P
    Z3 = (u * u - YY - ZZ) % P
    Y3 = (M * ((S - T) % P) - 8 * YYYY) % P
    return (T, Y3, Z3)


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
    S1 = (Y1 * ((Z2 * Z2Z2) % P)) % P
    S2 = (Y2 * ((Z1 * Z1Z1) % P)) % P
    if U1 == U2:
        if S1 != S2:
            return _JAC_INF          # p1 + (-p1) = identity
        return _jac_double(p1)       # p1 == p2
    H = (U2 - U1) % P
    HH2 = (2 * H) % P
    I = (HH2 * HH2) % P
    J = (H * I) % P
    r = (2 * ((S2 - S1) % P)) % P
    V = (U1 * I) % P
    X3 = (r * r - J - 2 * V) % P
    Y3 = (r * ((V - X3) % P) - 2 * ((S1 * J) % P)) % P
    zsum = (Z1 + Z2) % P
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


def scalar_mult(k, point):
    if k % N == 0 or point is INF:
        return INF
    if k < 0:
        x, y = point
        return scalar_mult(-k, (x, (-y) % P))
    # Right-to-left double-and-add (same structure as the original affine loop, which avoids
    # int.bit_length(), not available on MicroPython, just with the point ops in Jacobian).
    R = _JAC_INF
    addend = (point[0] % P, point[1] % P, 1)     # affine base -> Jacobian (Z = 1)
    while k:
        if k & 1:
            R = _jac_add(R, addend)
        addend = _jac_double(addend)
        k >>= 1
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
    point = point_add(scalar_mult(u1, G), scalar_mult(u2, Q))
    if point is INF:
        return False
    return point[0] % N == r

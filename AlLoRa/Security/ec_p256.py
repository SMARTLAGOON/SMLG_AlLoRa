"""Minimal pure-Python P-256 (secp256r1) for the ECDH handshake.

Dependency-free elliptic-curve math so ephemeral-static ECDH runs on the AlLoRa firmware
with no native crypto module — the asymmetric cost is paid once per session (a few hundred
ms of scalar multiplication on-device), never per frame. Consolidated from the project's
SecureAlLoRa reference implementation (the two hand-rolled P-256 files merged into one),
carrying only what ECDH needs: keypair generation, SEC1 uncompressed points, on-curve
validation, and the shared-secret computation. ECDSA verification for the control-root
downlink is a separate, later addition.

Public keys are always validated to be real points on the curve before use — accepting an
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


def scalar_mult(k, point):
    if k % N == 0 or point is INF:
        return INF
    if k < 0:
        x, y = point
        return scalar_mult(-k, (x, (-y) % P))
    result = INF
    addend = point
    while k:
        if k & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        k >>= 1
    return result


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

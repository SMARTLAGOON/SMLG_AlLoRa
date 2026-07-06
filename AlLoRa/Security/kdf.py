"""Derive the session keys from the ECDH shared secret (HKDF-SHA256).

The raw ECDH output (a curve X coordinate) is not usable key material directly, and one
secret must yield several independent keys. HKDF-SHA256 — the standard extract-then-expand
KDF — stretches the secret and separates it into an AES encryption key, a distinct HMAC
key, and a per-session nonce prefix, all cryptographically independent.

The construction here (the info label and the nonce-prefix length) is *provisional*: it is
a defensible default, but the exact bytes are a published surface deliberately left for a
crypto-review pass, so it is not treated as frozen. The nonce prefix pairs with the 2-byte
frame counter to form the AEAD nonce; its length is set so the assembled nonce lands at a
conventional 12 bytes.
"""
import hmac
import hashlib

ENC_KEY_LEN = 16       # AES-128
MAC_KEY_LEN = 16       # HMAC-SHA256 key
NONCE_PREFIX_LEN = 10  # + the 2-byte frame counter -> a 12-byte nonce (provisional)

_INFO = b"AlLoRa-v3-secure-session"


def hkdf_sha256(ikm, length, salt=b"", info=b""):
    """RFC 5869 HKDF with SHA-256: extract a pseudorandom key from ``ikm`` then expand it to
    ``length`` bytes bound to ``info``."""
    if salt == b"":
        # HashLen for SHA-256 is 32. Hardcoded because MicroPython's hashlib hash objects don't
        # expose `.digest_size` (it is a CPython attribute) — reading it degrades the handshake
        # on-device while CI stays green.
        salt = b"\x00" * 32
    prk = hmac.new(bytes(salt), bytes(ikm), hashlib.sha256).digest()   # extract
    out = b""
    block = b""
    counter = 1
    while len(out) < length:                                          # expand
        block = hmac.new(prk, block + bytes(info) + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def derive_session_keys(shared_secret, info=_INFO):
    """Return ``(enc_key, mac_key, nonce_prefix)`` derived from the ECDH shared secret. The
    two keys are combined into the Session's opaque ``key`` by the handshake; the frame layer
    splits them back out when it calls the AEAD."""
    material = hkdf_sha256(shared_secret, ENC_KEY_LEN + MAC_KEY_LEN + NONCE_PREFIX_LEN, info=info)
    enc_key = material[:ENC_KEY_LEN]
    mac_key = material[ENC_KEY_LEN:ENC_KEY_LEN + MAC_KEY_LEN]
    nonce_prefix = material[ENC_KEY_LEN + MAC_KEY_LEN:]
    return enc_key, mac_key, nonce_prefix


def derive_session_material(shared_secret, info=_INFO):
    """Return ``(enc_key, mac_key, prefix_ab, prefix_ba)``: the shared AES + HMAC keys, plus a
    **per-direction nonce prefix** — one for initiator->responder frames, one for the reverse.

    Both directions' send counters start at 1, so a single shared nonce prefix would make the
    first frame each way reuse ``(key, nonce)`` — a keystream-reuse footgun. Two prefixes keep
    the two directions' nonce spaces disjoint, which is all AES-CTR needs, and (because the
    prefix is bound into the HMAC) also makes a reflected frame fail the tag. This is the lean
    fix: **one key schedule**, not two (per-direction *keys* would add isolation that is
    worthless when the session is per-peer anyway), and **zero wire cost** — the prefixes are
    derived, never transmitted, exactly like the key.

    Provisional layout: the ordering (init prefix first) and the info label are a crypto-review
    surface, not frozen — property-tested, not byte-pinned.
    """
    length = ENC_KEY_LEN + MAC_KEY_LEN + 2 * NONCE_PREFIX_LEN
    material = hkdf_sha256(shared_secret, length, info=info)
    i = 0
    enc_key = material[i:i + ENC_KEY_LEN]; i += ENC_KEY_LEN
    mac_key = material[i:i + MAC_KEY_LEN]; i += MAC_KEY_LEN
    prefix_ab = material[i:i + NONCE_PREFIX_LEN]; i += NONCE_PREFIX_LEN
    prefix_ba = material[i:i + NONCE_PREFIX_LEN]
    return enc_key, mac_key, prefix_ab, prefix_ba

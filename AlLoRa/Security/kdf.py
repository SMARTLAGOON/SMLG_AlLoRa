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
        salt = b"\x00" * hashlib.sha256().digest_size
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

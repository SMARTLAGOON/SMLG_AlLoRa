"""Per-frame authenticated encryption: AES-128-CTR + truncated HMAC-SHA256.

The construction is encrypt-then-MAC with a 4-byte tag, using only *native* primitives
(hardware AES-CTR + SHA-256) so it stays sub-millisecond per frame on the ESP32 and never
threatens the receive->reply turnaround the adaptive pacing depends on. It is preferred
over AES-GCM because CTR + HMAC needs no GHASH (no per-release C module to maintain) and
its nonce-reuse failure mode is bounded rather than catastrophic.

This module is a *pure crypto primitive*: seal()/open() are handed already-assembled
``nonce`` and ``aad`` bytes and never interpret their layout, so nothing about the wire
byte layout is decided here (that assembly lives at the frame layer and is still subject to
a crypto-review pass). ``open()`` returns the plaintext on success or ``None`` on any
authentication failure, and it verifies the tag *before* decrypting so a forged frame never
produces plaintext.

The AES-CTR primitive is platform-detected and injected, so the same construction runs on
CPython (the ``cryptography`` lib) and on the AlLoRa MicroPython firmware (``ucryptolib``
with the CTR build flag). ``detect_aead()`` returns None when no native AES is present —
the graceful-degradation signal that secure mode is unavailable on this platform.
"""
import hmac
import hashlib


class AEAD:
    """Authenticated-encryption seam. Subclasses implement one construction.

    seal(enc_key, mac_key, nonce, aad, plaintext) -> ciphertext||tag
    open(enc_key, mac_key, nonce, aad, sealed)    -> plaintext, or None on auth failure
    """

    TAG_LEN = 4

    def seal(self, enc_key, mac_key, nonce, aad, plaintext):
        raise NotImplementedError

    def open(self, enc_key, mac_key, nonce, aad, sealed):
        raise NotImplementedError


class Ctr_hmac_aead(AEAD):
    """AES-CTR + truncated HMAC-SHA256, encrypt-then-MAC. The AES-CTR primitive is injected
    (a callable ``ctr(key, nonce, data) -> bytes``; CTR is symmetric, so it both encrypts
    and decrypts) so the construction is shared across platforms."""

    TAG_LEN = 4

    def __init__(self, ctr_cipher):
        self._ctr = ctr_cipher

    def seal(self, enc_key, mac_key, nonce, aad, plaintext):
        ciphertext = self._ctr(enc_key, nonce, plaintext)
        return ciphertext + self._tag(mac_key, nonce, aad, ciphertext)

    def open(self, enc_key, mac_key, nonce, aad, sealed):
        if len(sealed) < self.TAG_LEN:
            return None
        ciphertext, tag = sealed[:-self.TAG_LEN], sealed[-self.TAG_LEN:]
        if not hmac.compare_digest(tag, self._tag(mac_key, nonce, aad, ciphertext)):
            return None                     # auth failed -> never decrypt (verify before decrypt)
        return self._ctr(enc_key, nonce, ciphertext)

    def _tag(self, mac_key, nonce, aad, ciphertext):
        # The tag authenticates nonce || aad || ciphertext, truncated to TAG_LEN. Binding the
        # nonce and the aad (the frame header) means neither can be forged without detection.
        full = hmac.new(bytes(mac_key), bytes(nonce) + bytes(aad) + bytes(ciphertext),
                        hashlib.sha256).digest()
        return full[:self.TAG_LEN]


def _ctr_block(nonce):
    # Expand the assembled nonce into a 16-byte AES-CTR initial counter block: the nonce sits
    # high, the low bytes are the per-block counter (0, 1, 2, ...). Local to the crypto, never
    # transmitted. Provisional alongside the deferred nonce byte layout.
    return (bytes(nonce) + b"\x00" * 16)[:16]


def _cryptography_ctr():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except Exception:
        return None

    def ctr(key, nonce, data):
        c = Cipher(algorithms.AES(bytes(key)), modes.CTR(_ctr_block(nonce)))
        enc = c.encryptor()
        return enc.update(bytes(data)) + enc.finalize()

    return ctr


def _ucryptolib_ctr():
    try:
        import ucryptolib
    except Exception:
        return None

    MODE_CTR = 6  # ucryptolib AES-CTR (needs MICROPY_PY_UCRYPTOLIB_CTR in the board recipe)

    def ctr(key, nonce, data):
        return ucryptolib.aes(bytes(key), MODE_CTR, _ctr_block(nonce)).encrypt(bytes(data))

    return ctr


def _detect_ctr():
    return _cryptography_ctr() or _ucryptolib_ctr()


def detect_aead():
    """Return an AEAD backed by native AES-CTR, or None if this platform has none.

    None is the graceful-degradation signal: secure mode is unavailable and the caller
    decides posture — an operational (registered) node must refuse to run, a test node may
    fall back to open mode.
    """
    ctr = _detect_ctr()
    if ctr is None:
        return None
    return Ctr_hmac_aead(ctr)

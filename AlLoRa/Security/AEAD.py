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


def _ct_equal(a, b):
    # Constant-time byte-string comparison. hmac.compare_digest is CPython-only — MicroPython's
    # hmac module (micropython-lib) doesn't provide it — so depending on it degrades secure mode
    # on-device even though everything imports. This runs in time that depends only on len(a),
    # not on where the bytes first differ, so a forged tag can't be reconstructed byte-by-byte
    # from response timing. Both operands here are always the fixed TAG_LEN, so the length branch
    # leaks nothing useful.
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(bytes(a), bytes(b)):
        result |= x ^ y
    return result == 0


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
        if not _ct_equal(tag, self._tag(mac_key, nonce, aad, ciphertext)):
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


def _micropython_ctr():
    # MicroPython's native AES module was renamed ``ucryptolib`` -> ``cryptolib`` in v1.21 (the
    # u-module unification, the same change that renamed the CTR build flag). Unlike ubinascii /
    # utime / ujson, ``ucryptolib`` was never in the weak-link alias table, so on a v1.21+ build
    # ``import ucryptolib`` raises and there is no fallback — the module is only reachable as
    # ``cryptolib``. Prefer the new name; keep the old one for pre-1.21 firmware.
    try:
        import cryptolib as aes_mod
    except ImportError:
        try:
            import ucryptolib as aes_mod
        except Exception:
            return None
    except Exception:
        return None

    MODE_CTR = 6  # AES-CTR (needs MICROPY_PY_CRYPTOLIB_CTR compiled into the board)

    def ctr(key, nonce, data):
        return aes_mod.aes(bytes(key), MODE_CTR, _ctr_block(nonce)).encrypt(bytes(data))

    return ctr


def _detect_ctr():
    return _cryptography_ctr() or _micropython_ctr()


def _self_test(aead):
    """Confirm the backend actually seals and opens. Importing ``ucryptolib`` does not prove
    AES-CTR is compiled in (it needs the CTR build flag), so a backend that *looks* present
    can still throw on first use — a one-shot round-trip catches that here."""
    try:
        key = bytes(16)
        sealed = aead.seal(key, key, bytes(12), b"", b"probe")
        return aead.open(key, key, bytes(12), b"", sealed) == b"probe"
    except Exception:
        return False


def detect_aead():
    """Return an AEAD backed by working native AES-CTR, or None if this platform has none.

    None is the graceful-degradation signal: secure mode is unavailable and the caller
    decides posture — an operational (registered) node must refuse to run, a test node may
    fall back to open mode. The backend is *self-tested* before it is returned, so a platform
    where AES-CTR imports but is not compiled in degrades cleanly instead of crashing later.
    """
    ctr = _detect_ctr()
    if ctr is None:
        return None
    aead = Ctr_hmac_aead(ctr)
    return aead if _self_test(aead) else None


def unavailable_reason():
    """A precise explanation of why ``detect_aead()`` returned None, for the degraded-mode log
    line. Best-effort and only meant for the degrade path (it re-runs the cheap detection). It
    exercises each stage of the seal/open self-test in isolation and reports the actual
    exception, so a degrade on real hardware names its own cause on the boot log — no REPL
    needed, which matters because the radio loop is hard to interrupt for one.
    """
    ctr = _detect_ctr()
    if ctr is None:
        return "native AES module did not import (need 'cryptolib'; v1.21+ dropped 'ucryptolib')"
    # Stage 1: AES-CTR (mode 6) in isolation — a real encrypt+decrypt round-trip.
    try:
        pt = b"sixteen bytes!!!"
        ct = ctr(bytes(16), bytes(12), pt)
        if ctr(bytes(16), bytes(12), ct) != pt:
            return "AES-CTR ran but did not round-trip (mode 6 not real CTR)"
    except Exception as e:
        return "AES-CTR (mode 6) raised: {} (CTR not compiled?)".format(repr(e))
    # Stage 2: the HMAC-SHA256 tag in isolation.
    try:
        hmac.new(bytes(16), b"probe", hashlib.sha256).digest()
    except Exception as e:
        return "HMAC-SHA256 raised: {} (hmac/hashlib backend?)".format(repr(e))
    # Stage 3: the full seal/open (catches assembly issues like a missing compare_digest).
    if not _self_test(Ctr_hmac_aead(ctr)):
        return "AES-CTR + HMAC both work in isolation but seal/open round-trip failed"
    return "AEAD available"

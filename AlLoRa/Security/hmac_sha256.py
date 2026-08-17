"""HMAC-SHA256 (RFC 2104), written out against the native hash.

MicroPython ships no built-in HMAC. The firmware manifest pulls micropython-lib's pure-Python
one, and that wrapper costs 19.4 ms on the Edge and 48.1 ms on the Hub against 0.14 ms for the
SHA-256 it wraps. Two callers pay for it, and one of them pays four times per radio round: the
per-frame AEAD tag, which is what made a sealed transfer cost about 28% more than an open one
instead of about 4%. The other is the session key schedule, charged once per handshake.

HMAC is two hash passes over key-derived pads, so writing it here costs a dozen lines and
returns the same bytes as the module it replaces, at 1.1 ms on both boards. It lives in its own
module rather than inside either caller because both are primitives: an AEAD should not have to
import a key-derivation module to seal a frame, and a key schedule should not have to import a
cipher to stretch a secret.

Everything here is byte-identical to ``hmac.new(key, msg, hashlib.sha256).digest()``. That is a
property the tests pin against the standard library and against the RFC 5869 vectors, not an
aspiration: a MAC that is merely fast authenticates nothing.
"""
import hashlib

BLOCK_SIZE = 64      # SHA-256's block size, and therefore the width of the RFC 2104 pads
DIGEST_SIZE = 32     # hardcoded because MicroPython hash objects expose no .digest_size


def hmac_sha256(key, msg):
    """Return the full 32-byte HMAC-SHA256 of ``msg`` under ``key``.

    ``key`` and ``msg`` must be bytes-like. Callers that want a truncated tag slice the result
    themselves, so nothing here decides a tag length.
    """
    k = bytes(key)
    if len(k) > BLOCK_SIZE:
        k = hashlib.sha256(k).digest()          # RFC 2104: a key longer than a block is hashed
    k = k + b"\x00" * (BLOCK_SIZE - len(k))     # and any key shorter than one is zero-padded
    inner_pad = bytearray(BLOCK_SIZE)
    outer_pad = bytearray(BLOCK_SIZE)
    for n in range(BLOCK_SIZE):
        c = k[n]
        inner_pad[n] = c ^ 0x36
        outer_pad[n] = c ^ 0x5C
    # The message is fed with update() rather than concatenated onto the pad, so a chunk is not
    # copied into a second buffer four times a round on a heap this small. update() is safe on
    # the device for a reason worth writing down: the pure-Python hmac this replaces is built on
    # it, so every sealed frame sent so far has already exercised that path on these boards.
    inner = hashlib.sha256(bytes(inner_pad))
    inner.update(msg)
    outer = hashlib.sha256(bytes(outer_pad))
    outer.update(inner.digest())
    return outer.digest()

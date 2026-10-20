"""HMAC-SHA256 (RFC 2104), built on the board's native SHA-256.

It gives the same bytes as `hmac.new(key, msg, hashlib.sha256).digest()`. Use it instead of the
`hmac` module: on the boards, MicroPython's version is 17 to 44 times slower.
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

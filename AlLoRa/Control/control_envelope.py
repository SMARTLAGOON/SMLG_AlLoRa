"""The byte layout of a signed control command. The signer and the checker both use it.

    version(1) || type(1) || target_device_id(32) || counter(4) || payload || sig(64)
    sig = ECDSA-P256 over SHA-256(everything before it), raw r || s.
A node refuses a counter that is not higher than the last one, so an old command cannot be replayed.
"""

# Bumped only when the byte layout changes. Version 1 had no counter, so an artifact minted
# for it is refused here rather than reinterpreted: a length that happens to parse under the
# wrong layout would silently read someone else's bytes as a counter.
ENVELOPE_VERSION = 2

SIG_LEN = 64
TARGET_LEN = 32
COUNTER_LEN = 4
HEADER_LEN = 1 + 1 + TARGET_LEN + COUNTER_LEN   # version, type, target_device_id, counter
MIN_LEN = HEADER_LEN + SIG_LEN                  # the fixed overhead with an empty payload
MAX_COUNTER = (1 << (8 * COUNTER_LEN)) - 1


def signed_region(control_type, target_device_id, counter, payload=b"",
                  version=ENVELOPE_VERSION):
    """Everything a control artifact signs over, which is everything but the signature."""
    return (bytes([version, control_type]) + bytes(target_device_id)
            + counter.to_bytes(COUNTER_LEN, "big") + bytes(payload))

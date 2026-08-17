"""Can a damaged frame parse as a valid frame? In open mode, yes: its header is not covered.

This answers the question the payload-CRC bug left open, and the answer is what makes the
radio's CRC load-bearing rather than a tidier duplicate of a check AlLoRa already has.

Open-mode v3 carries a 24-bit integrity trailer, and it is computed over the **payload alone**
(`sha256(payload)[:3]`). So the trailer says nothing about the six header bytes in front of it,
and the only header damage framing rejects is damage that happens to make the frame
unreadable: a version nibble that stops saying 3, or a kind nibble that lands on a code no
version defines. Everything else is accepted as a valid frame that means something other than
what was sent. Of the 24 single-bit errors available in a sid-addressed header, 19 parse.

The sharpest case is the typed-kind nibble: one flipped bit turns a CHUNK into a METADATA, a
DATA or an ACKMAP, and framing hands it up as genuine. ADR 0017 opens on the observation that
many honest frames can assemble into a dishonest file; this is its mirror, a frame that is not
honest and, until the receive path read the modem's flag, was not checked either.

The sealed posture does not have this hole. Its header is the AEAD's AAD, so every bit of it is
authenticated, and all 576 single-bit errors in a sealed frame are refused. The gap between the
two postures is the point: in open mode the modem's payload CRC is the only check covering the
whole frame, header included, which is why reading that flag is a correctness fix rather than
housekeeping.

**This records what the wire does today, not what it should do.** Widening the trailer to cover
the header is a wire-format change on a published, citable surface, so it is a decision to take
deliberately and not a defect to patch. When it is taken, these counts are meant to fail.
"""
import pytest

from AlLoRa.Codec import V3OpenCodec
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.AEAD import detect_aead
from AlLoRa.Security.Session import Session

SID = 0x2A
PAYLOAD = bytes((i * 7) % 256 for i in range(64))

# Byte offsets in the sid-addressed P2P header: !BBB3s = sid, VT, FL, integrity.
SID_BYTE = 0
VT_BYTE = 1
FL_BYTE = 2
INTEGRITY = (3, 4, 5)
HEADER_SIZE = Packet_v3.HEADER_SIZE_SID_P2P

KEY = bytes(range(32))                  # enc(16) || mac(16)
SEND_PREFIX = bytes(range(10))
RECV_PREFIX = bytes(range(10, 20))


def _chunk():
    p = Packet_v3(addressing="sid")
    p.set_session(SID)
    p.set_kind(Packet_v3.CHUNK)
    p.set_payload(PAYLOAD)
    return p


def _open_wire():
    return _chunk().get_content()


def _flip(wire, index, bit):
    damaged = bytearray(wire)
    damaged[index] ^= 1 << bit
    return bytes(damaged)


def _parses(wire):
    return V3OpenCodec(addressing="sid").deframe(wire) is not None


def _survivors(indexes):
    wire = _open_wire()
    return sum(1 for i in indexes for bit in range(8) if _parses(_flip(wire, i, bit)))


# --- what the integrity trailer does cover ----------------------------------------------

def test_no_single_bit_of_payload_damage_parses():
    payload_bytes = range(HEADER_SIZE, len(_open_wire()))

    assert _survivors(payload_bytes) == 0


def test_no_single_bit_of_damage_to_the_trailer_itself_parses():
    assert _survivors(INTEGRITY) == 0


def test_damage_to_the_version_nibble_is_rejected():
    # The one part of the header that is checked, and only incidentally: a frame that no
    # longer claims version 3 cannot be read at all.
    wire = _open_wire()

    for bit in range(4, 8):
        assert not _parses(_flip(wire, VT_BYTE, bit))


# --- what it does not ---------------------------------------------------------------------

@pytest.mark.parametrize("region, indexes, expected", [
    # Every value is a legal session id, so every bit of it is a legal frame addressed elsewhere.
    ("session id", (SID_BYTE,), 8),
    # Seven flags plus one unused bit, none of them checked by anything.
    ("flag byte", (FL_BYTE,), 8),
    # CHUNK is 0x2, so three flips land on another defined kind (METADATA, DATA, ACKMAP) and
    # the rest fall outside the table or hit the version nibble above.
    ("typed kind", (VT_BYTE,), 3),
])
def test_header_damage_parses_as_a_valid_frame(region, indexes, expected):
    assert _survivors(indexes) == expected, region


def test_the_whole_header_taken_together():
    # 19 of 24. The figure is here so a change to the trailer's coverage has to come past it.
    assert _survivors(range(HEADER_SIZE - len(INTEGRITY))) == 19


def test_one_flipped_bit_turns_a_chunk_into_metadata():
    # The consequence in the shape that matters to a transfer: a collector that asked for a
    # chunk is handed a frame it reads as the file's metadata, and every check the frame
    # carries agrees it is genuine.
    damaged = _flip(_open_wire(), VT_BYTE, 0)

    parsed = V3OpenCodec(addressing="sid").deframe(damaged)

    assert parsed is not None
    assert parsed.get_command() == Packet_v3.METADATA
    assert parsed.get_payload() == PAYLOAD      # the chunk's bytes, now read as metadata


def test_a_flipped_flag_travels_as_an_instruction():
    # Flags are read, not decoration: `sleep` tells a node to power down after answering.
    original = _chunk()
    original.load(_open_wire())
    damaged = V3OpenCodec(addressing="sid").deframe(_flip(_open_wire(), FL_BYTE, 1))

    assert damaged is not None
    assert damaged.sleep != original.sleep


# --- the sealed posture, for contrast -----------------------------------------------------

def _sealed_wire():
    sender = Session(sid=SID, key=KEY,
                     send_nonce_prefix=SEND_PREFIX, recv_nonce_prefix=RECV_PREFIX)
    return _chunk().get_secure_content(sender, detect_aead())


def _opens(wire):
    # A fresh receiver each time, so what refuses the frame is the tag rather than the
    # anti-replay window having already seen this counter.
    receiver = Session(sid=SID, key=KEY,
                       send_nonce_prefix=RECV_PREFIX, recv_nonce_prefix=SEND_PREFIX)
    packet = Packet_v3(addressing="sid")
    try:
        return bool(packet.load_secure(wire, receiver, detect_aead()))
    except Exception:
        return False


def test_the_sealed_posture_covers_its_header_too():
    # sid, version+kind and the anti-replay counter are the AEAD's AAD, so damage to any of
    # them breaks the tag. This is the hole the open posture has and this one does not.
    wire = _sealed_wire()
    header = range(Packet_v3.SECURE_HEADER_SIZE_SID_P2P)

    assert sum(1 for i in header for bit in range(8) if _opens(_flip(wire, i, bit))) == 0


def test_no_single_bit_of_damage_to_a_sealed_frame_opens():
    wire = _sealed_wire()

    assert sum(1 for i in range(len(wire)) for bit in range(8) if _opens(_flip(wire, i, bit))) == 0

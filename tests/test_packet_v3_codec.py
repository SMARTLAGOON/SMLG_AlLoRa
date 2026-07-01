"""v3 typed Packet codec — the wire structure.

These tests pin the v3 frame *before* the engine speaks it. The chosen layout — a
versioned type byte, sid-addressed — is:

  MAC-addressed (handshake / first contact):
      [src4][dst4][VT1][FL1][integ3]              = 13 B  (P2P, non-mesh)
  session-id-addressed (established session):
      [sid1][VT1][FL1][integ3]                    =  6 B  (P2P, non-mesh)

  VT   = version nibble (0x3) | typed-kind nibble (DATA/OK/CHUNK/METADATA/GRANT/CTRL/ACKMAP)
  FL   = mesh|sleep|hop|debug_hops|role_token|auth|cfg_epoch|spare
  integ = sha256(payload)[:3]  -> a *real* 24-bit digest (v2 stored 12 bits of hex here)

Typed payloads (replace v2's stringly-typed ones):
  METADATA : chunk_size(2 LE) | total_len(4 LE) | filename(utf-8)   (typed; v2 sent only chunk_count)
  CHUNK    : chunk_index(2 LE)                                       (binary, not ASCII decimal)
  DATA     : raw chunk bytes (no index — stop-and-wait hot path)
"""
import struct

import pytest

from AlLoRa.Packet_v3 import Packet_v3

SRC = "a1a1a1a1"
DST = "b2b2b2b2"


def _fresh(addressing, mesh_mode=False):
    return Packet_v3(mesh_mode=mesh_mode, addressing=addressing)


# --- version + typed kind ---------------------------------------------------

def test_version_nibble_is_3_and_kind_round_trips_sid():
    p = _fresh("sid")
    p.set_session(7)
    p.set_data(b"hello world")

    wire = p.get_content()

    got = _fresh("sid")
    assert got.load(wire) is True
    assert got.get_version() == 0x3
    assert got.get_command() == "DATA"
    assert got.get_session() == 7
    assert got.get_payload() == b"hello world"


@pytest.mark.parametrize("kind", ["DATA", "OK", "CHUNK", "METADATA", "GRANT", "CTRL", "ACKMAP"])
def test_all_typed_kinds_round_trip(kind):
    p = _fresh("sid")
    p.set_session(1)
    p.set_kind(kind)
    p.set_payload(b"\x01\x02")

    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    assert got.get_command() == kind


def test_wrong_version_nibble_fails_load():
    p = _fresh("sid")
    p.set_session(1)
    p.set_data(b"x")
    wire = bytearray(p.get_content())
    # VT byte is at offset 1 in sid framing; stomp the version nibble to v2 (0x2).
    wire[1] = (0x2 << 4) | (wire[1] & 0x0F)
    got = _fresh("sid")
    assert got.load(bytes(wire)) is False


# --- integrity: a real 24-bit digest, header sizes --------------------------

def test_integrity_is_24_real_bits_and_catches_corruption():
    p = _fresh("sid")
    p.set_session(3)
    p.set_data(bytes(range(40)))
    wire = bytearray(p.get_content())

    # flip one payload byte -> integrity must reject it
    wire[-1] ^= 0xFF
    got = _fresh("sid")
    assert got.load(bytes(wire)) is False


def test_sid_and_mac_header_sizes_match_layout_1():
    # sid-addressed DATA header = 6 B
    p = _fresh("sid")
    p.set_session(9)
    p.set_data(b"abcdef")
    assert len(p.get_content()) - len(b"abcdef") == 6

    # MAC-addressed DATA header = 13 B
    q = _fresh("mac")
    q.set_source(SRC)
    q.set_destination(DST)
    q.set_data(b"abcdef")
    assert len(q.get_content()) - len(b"abcdef") == 13


# --- MAC addressing (handshake / first contact) -----------------------------

def test_mac_addressing_round_trips_short_macs():
    p = _fresh("mac")
    p.set_source(SRC)
    p.set_destination(DST)
    p.set_ok()

    got = _fresh("mac")
    assert got.load(p.get_content()) is True
    assert got.get_source() == SRC
    assert got.get_destination() == DST
    assert got.get_command() == "OK"


# --- typed METADATA ---------------------------------------------------------

def test_metadata_carries_chunk_size_total_len_and_filename():
    p = _fresh("sid")
    p.set_session(2)
    p.set_metadata(chunk_size=243, total_len=1000, filename="payload.bin")

    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    md = got.get_metadata()
    assert md["CHUNK_SIZE"] == 243
    assert md["TOTAL_LEN"] == 1000
    assert md["FILENAME"] == "payload.bin"
    # chunk_count is derived, not on the wire: ceil(1000 / 243) == 5
    assert md["LENGTH"] == 5


# --- typed (binary) CHUNK request index -------------------------------------

def test_chunk_request_index_is_binary_two_bytes():
    p = _fresh("sid")
    p.set_session(2)
    p.ask_data(2)
    assert p.get_payload() == (2).to_bytes(2, "little")

    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    assert got.get_command() == "CHUNK"
    assert got.get_chunk_index() == 2


def test_chunk_index_handles_values_v2_ascii_would_bloat():
    # index 5000 is 2 binary bytes here; v2's ASCII "5000" was 4.
    p = _fresh("sid")
    p.set_session(4)
    p.ask_data(5000)
    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    assert got.get_chunk_index() == 5000


# --- FL flag byte -----------------------------------------------------------

def test_role_token_flag_round_trips_without_disturbing_kind():
    # The role-swap "I have data" bit rides the responder's normal replies -> a DATA
    # frame must carry it while staying a DATA frame.
    p = _fresh("sid")
    p.set_session(5)
    p.set_data(b"chunk")
    p.set_role_token(True)

    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    assert got.get_command() == "DATA"
    assert got.get_role_token() is True


def test_flags_default_clear():
    p = _fresh("sid")
    p.set_session(6)
    p.set_data(b"x")
    got = _fresh("sid")
    assert got.load(p.get_content()) is True
    assert got.get_role_token() is False
    assert got.get_mesh() is False

"""Unit — Codec, the seam that speaks a Packet on the wire.

Framing/version/posture and reply-matching used to be scattered across the Connector
(`_new_response_packet` / `_response_matches`) and a parallel method-pair on Packet_v3
(open `get_content`/`load` vs secure `get_secure_content`/`load_secure`). These pin the
extracted seam: one `frame`/`deframe`/`match_spec` interface with an implementation per
(version x posture), the secure pair folded inside `V3SecureCodec` as private mechanism.

The engine calls `frame`/`deframe`/`match_spec` identically for every posture — the secure
codec resolves the per-peer session from the cleartext sid — so nothing above the seam ever
branches on open vs secure. The transfer tests (test_loopback_transfer / test_v3_open_transfer)
are the end-to-end proof over the live engine; these are the focused unit checks.
"""
import pytest

from AlLoRa.Codec import V2Codec, V3OpenCodec, V3SecureCodec, build_codec
from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.Session import Session
from AlLoRa.Security.AEAD import detect_aead


# --- V2Codec (legacy MAC-addressed frame + checksum) ---------------------------------------

def _v2(src, dst):
    p = Packet(mesh_mode=False, short_mac=True)
    p.set_source(src)
    p.set_destination(dst)
    p.set_ok()
    return p


def test_v2_frame_deframe_round_trips():
    codec = V2Codec(short_mac=True)
    got = codec.deframe(codec.frame(_v2("a1a1a1a1", "b2b2b2b2")))
    assert got is not None
    assert got.get_source() == "a1a1a1a1"
    assert got.get_destination() == "b2b2b2b2"
    assert got.get_command() == Packet.OK


def test_v2_deframe_rejects_garbage():
    assert V2Codec(short_mac=True).deframe(b"\x00\x01\x02") is None


def test_v2_match_spec_accepts_the_mirrored_reply():
    codec = V2Codec(short_mac=True, my_mac="b2b2b2b2")
    spec = codec.match_spec(_v2("b2b2b2b2", "a1a1a1a1"))   # I (b2) asked peer a1
    assert spec.matches(_v2("a1a1a1a1", "b2b2b2b2")) is True   # a1 replies to me


def test_v2_match_spec_rejects_a_foreign_reply():
    codec = V2Codec(short_mac=True, my_mac="b2b2b2b2")
    spec = codec.match_spec(_v2("b2b2b2b2", "a1a1a1a1"))
    assert spec.matches(_v2("cccccccc", "b2b2b2b2")) is False   # not from the peer we asked
    assert spec.matches(_v2("a1a1a1a1", "dddddddd")) is False   # not addressed to us


def test_v2_match_spec_matches_on_the_wire():
    # The same MAC-mirror check, applied to the raw frame prefix (src bytes then dst bytes)
    # — what a keyless bridge runs at the radio.
    codec = V2Codec(short_mac=True, my_mac="b2b2b2b2")
    spec = codec.match_spec(_v2("b2b2b2b2", "a1a1a1a1"))
    assert spec.matches_wire(codec.frame(_v2("a1a1a1a1", "b2b2b2b2"))) is True
    assert spec.matches_wire(codec.frame(_v2("cccccccc", "b2b2b2b2"))) is False


# --- V3OpenCodec (sid-addressed typed frame + 24-bit integrity) ----------------------------

def _v3_data(sid, chunk):
    p = Packet_v3(addressing="sid")
    p.set_session(sid)
    p.set_data(chunk)
    return p


def test_v3_open_frame_deframe_round_trips():
    codec = V3OpenCodec(addressing="sid")
    got = codec.deframe(codec.frame(_v3_data(7, b"hello")))
    assert got is not None
    assert got.get_session() == 7
    assert got.get_command() == Packet_v3.DATA
    assert got.get_payload() == b"hello"


def test_v3_open_deframe_rejects_corrupt_integrity():
    codec = V3OpenCodec(addressing="sid")
    wire = bytearray(codec.frame(_v3_data(7, b"hello")))
    wire[-1] ^= 0xFF                                  # mutate the payload -> integrity breaks
    assert codec.deframe(bytes(wire)) is None


def test_v3_match_spec_is_by_session():
    codec = V3OpenCodec(addressing="sid")
    req = Packet_v3(addressing="sid"); req.set_session(9); req.ask_metadata()
    spec = codec.match_spec(req)
    reply_ok = Packet_v3(addressing="sid"); reply_ok.set_session(9); reply_ok.set_ok()
    reply_bad = Packet_v3(addressing="sid"); reply_bad.set_session(3); reply_bad.set_ok()
    assert spec.matches(reply_ok) is True
    assert spec.matches(reply_bad) is False


def test_v3_match_spec_matches_on_the_wire():
    # The sid is cleartext at wire offset 0, so the reply-match runs on the prefix — keyless,
    # and identical for open and secure frames.
    codec = V3OpenCodec(addressing="sid")
    req = Packet_v3(addressing="sid"); req.set_session(9); req.ask_metadata()
    spec = codec.match_spec(req)
    assert spec.matches_wire(codec.frame(_v3_data(9, b"x"))) is True
    assert spec.matches_wire(codec.frame(_v3_data(3, b"x"))) is False
    assert spec.matches_wire(b"") is False


# --- V3SecureCodec (the secure pair folded in; session resolved from the cleartext sid) ----

SID = 42
KEY = bytes(range(32))               # enc(16) || mac(16)
NONCE_PREFIX = bytes(range(10))


def _secure_pair():
    """Two ends of one handshake: identical key material, independent send/recv sessions."""
    aead = detect_aead()
    sender = Session(sid=SID, key=KEY, nonce_prefix=NONCE_PREFIX)
    receiver = Session(sid=SID, key=KEY, nonce_prefix=NONCE_PREFIX)
    return (V3SecureCodec(lambda sid: sender, aead),
            V3SecureCodec(lambda sid: receiver, aead), aead)


def test_secure_frame_deframe_round_trips():
    send_codec, recv_codec, _ = _secure_pair()
    got = recv_codec.deframe(send_codec.frame(_v3_data(SID, b"reading")))
    assert got is not None
    assert got.get_session() == SID
    assert got.get_command() == Packet_v3.DATA
    assert got.get_payload() == b"reading"


def test_secure_deframe_rejects_a_tampered_frame():
    send_codec, recv_codec, _ = _secure_pair()
    wire = bytearray(send_codec.frame(_v3_data(SID, b"sensitive")))
    wire[-1] ^= 0x01                                  # flip a tag bit
    assert recv_codec.deframe(bytes(wire)) is None


def test_secure_deframe_rejects_a_replay():
    send_codec, recv_codec, _ = _secure_pair()
    wire = send_codec.frame(_v3_data(SID, b"once"))
    assert recv_codec.deframe(wire) is not None       # fresh -> accepted
    assert recv_codec.deframe(wire) is None           # identical bytes replayed -> rejected


def test_secure_deframe_rejects_an_unknown_session():
    aead = detect_aead()
    blind = V3SecureCodec(lambda sid: None, aead)     # resolver knows no sessions
    real = Session(sid=SID, key=KEY, nonce_prefix=NONCE_PREFIX)
    wire = _v3_data(SID, b"x").get_secure_content(real, aead)
    assert blind.deframe(wire) is None                # can't authenticate -> reject


def test_secure_frame_without_a_session_is_a_wiring_error():
    aead = detect_aead()
    codec = V3SecureCodec(lambda sid: None, aead)
    with pytest.raises(ValueError):
        codec.frame(_v3_data(SID, b"x"))


# --- build_codec (picks the implementation for version x posture) --------------------------

def test_build_codec_picks_v2_for_version_2():
    assert isinstance(build_codec(protocol_version=2, addressing="mac"), V2Codec)


def test_build_codec_picks_v3_open_by_default():
    assert isinstance(build_codec(protocol_version=3, addressing="sid"), V3OpenCodec)


def test_build_codec_picks_secure_when_fully_provisioned():
    codec = build_codec(protocol_version=3, addressing="sid", security_mode="secure",
                        session_resolver=lambda sid: None, aead=detect_aead())
    assert isinstance(codec, V3SecureCodec)


def test_build_codec_degrades_to_open_without_a_backend():
    # A secure posture but no AEAD backend / resolver falls back to open — the node has
    # already decided that is acceptable for its posture before reaching here.
    codec = build_codec(protocol_version=3, addressing="sid", security_mode="secure",
                        session_resolver=None, aead=None)
    assert isinstance(codec, V3OpenCodec)

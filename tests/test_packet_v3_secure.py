"""Unit — the v3 *secure* frame serialization (Packet_v3 seal/open).

Open mode ships a 24-bit integrity trailer; secure mode replaces it with an AEAD-sealed
payload plus the two things authenticated encryption needs on the wire: a monotonic frame
counter (anti-replay) and a 4-byte tag. The same typed header (sid, version+kind, flags) is
authenticated as the AEAD's AAD, so it cannot be forged.

This exercises the secure path as a parallel serialization on Packet_v3 — `get_secure_content`
/ `load_secure`, which take the session (for keys + counter + replay state) and the AEAD
backend as injected params. The open-mode `get_content` / `load` are untouched. The exact
secure header byte layout and the nonce assembly are a defensible *provisional* default,
deferred to a crypto-review pass; these tests assert behavior (round-trips, tamper caught,
replay caught), not exact bytes, so that review does not churn them.
"""
import struct

from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.Session import Session
from AlLoRa.Security.AEAD import detect_aead

SID = 42
KEY = bytes(range(32))          # enc(16) || mac(16)
NONCE_PREFIX = bytes(range(10))


def _sessions():
    # The two ends of one handshake hold sessions with identical key material; here we
    # construct them directly (the codec doesn't care how the keys were derived).
    sender = Session(sid=SID, key=KEY, nonce_prefix=NONCE_PREFIX)
    receiver = Session(sid=SID, key=KEY, nonce_prefix=NONCE_PREFIX)
    return sender, receiver


def _data_packet(chunk):
    p = Packet_v3(addressing="sid")
    p.set_session(SID)
    p.set_data(chunk)
    return p


def test_secure_frame_round_trips_kind_and_payload():
    sender, receiver = _sessions()
    aead = detect_aead()
    chunk = bytes(i % 256 for i in range(200))

    wire = _data_packet(chunk).get_secure_content(sender, aead)

    got = Packet_v3(addressing="sid")
    assert got.load_secure(wire, receiver, aead) is True
    assert got.get_command() == Packet_v3.DATA
    assert got.get_session() == SID
    assert got.get_payload() == chunk


def test_flags_survive_the_secure_round_trip():
    sender, receiver = _sessions()
    aead = detect_aead()
    p = _data_packet(b"x")
    p.set_role_token(True)                      # a header flag that must not be forgeable
    wire = p.get_secure_content(sender, aead)

    got = Packet_v3(addressing="sid")
    assert got.load_secure(wire, receiver, aead) is True
    assert got.get_role_token() is True


def test_a_tampered_secure_frame_is_rejected():
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = bytearray(_data_packet(b"sensitive").get_secure_content(sender, aead))
    wire[-1] ^= 0x01                            # flip a bit in the tag
    got = Packet_v3(addressing="sid")
    assert got.load_secure(bytes(wire), receiver, aead) is False


def test_a_forged_header_is_rejected():
    # The header is the AEAD's AAD; changing a flag byte breaks the tag even though the
    # ciphertext is untouched.
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = bytearray(_data_packet(b"sensitive").get_secure_content(sender, aead))
    wire[2] ^= 0x10                             # flip a bit in the FL (flags) byte
    got = Packet_v3(addressing="sid")
    assert got.load_secure(bytes(wire), receiver, aead) is False


def test_a_replayed_secure_frame_is_rejected():
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = _data_packet(b"one reading").get_secure_content(sender, aead)

    first = Packet_v3(addressing="sid")
    assert first.load_secure(wire, receiver, aead) is True   # fresh -> accepted
    second = Packet_v3(addressing="sid")
    assert second.load_secure(wire, receiver, aead) is False  # identical bytes replayed


def test_the_wire_counter_advances_per_frame():
    sender, _ = _sessions()
    aead = detect_aead()
    w1 = _data_packet(b"a").get_secure_content(sender, aead)
    w2 = _data_packet(b"b").get_secure_content(sender, aead)
    c1 = struct.unpack(Packet_v3.SECURE_HEADER_FORMAT_SID_P2P, w1[:Packet_v3.SECURE_HEADER_SIZE_SID_P2P])[3]
    c2 = struct.unpack(Packet_v3.SECURE_HEADER_FORMAT_SID_P2P, w2[:Packet_v3.SECURE_HEADER_SIZE_SID_P2P])[3]
    assert c1 == 1 and c2 == 2

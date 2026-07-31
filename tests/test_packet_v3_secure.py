"""Unit — the v3 *secure* frame serialization (Packet_v3 seal/open).

Open mode ships a 24-bit integrity trailer; secure mode replaces it with an AEAD-sealed
payload plus the two things authenticated encryption needs on the wire: a monotonic frame
counter (anti-replay) and a 4-byte tag. The typed header (sid, version+kind, counter) is
authenticated as the AEAD's AAD, so it cannot be forged. It carries no flag byte: secure mode
is sid-addressed P2P only, and every flag is either mesh-scoped or has no producer, so the
byte would have been a constant on the wire. The send side refuses to seal a frame whose
flags could not travel, rather than dropping them quietly.

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
NONCE_PREFIX = bytes(range(10))         # the sender's send prefix == the receiver's recv prefix
REVERSE_PREFIX = bytes(range(10, 20))   # the other direction (unused by these one-way tests)


def _sessions():
    # The two ends of one handshake: identical key material, and per-direction nonce prefixes
    # that agree (the sender's send prefix is the receiver's recv prefix, and vice versa).
    sender = Session(sid=SID, key=KEY,
                     send_nonce_prefix=NONCE_PREFIX, recv_nonce_prefix=REVERSE_PREFIX)
    receiver = Session(sid=SID, key=KEY,
                       send_nonce_prefix=REVERSE_PREFIX, recv_nonce_prefix=NONCE_PREFIX)
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


def test_sealing_a_flagged_frame_is_refused_rather_than_dropping_the_flag():
    # A secure frame has no flag byte, so a flag set here would go missing between the two
    # ends with nothing to show for it. The send side refuses instead, which is the wall a
    # future secure-mesh author should hit.
    sender, _ = _sessions()
    aead = detect_aead()
    p = _data_packet(b"x")
    p.set_role_token(True)

    try:
        p.get_secure_content(sender, aead)
    except ValueError:
        pass
    else:
        assert False, "a flag that cannot travel must raise, not vanish"


def test_the_default_flag_state_seals_and_opens():
    # `sleep` defaults on and is exempt from the guard: it is only read behind a mesh guard,
    # so in P2P it carries no instruction. Every ordinary frame is in this state.
    sender, receiver = _sessions()
    aead = detect_aead()
    p = _data_packet(b"x")
    assert p.get_sleep() is True

    wire = p.get_secure_content(sender, aead)
    got = Packet_v3(addressing="sid")
    assert got.load_secure(wire, receiver, aead) is True
    assert got.get_payload() == b"x"


def test_a_tampered_secure_frame_is_rejected():
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = bytearray(_data_packet(b"sensitive").get_secure_content(sender, aead))
    wire[-1] ^= 0x01                            # flip a bit in the tag
    got = Packet_v3(addressing="sid")
    assert got.load_secure(bytes(wire), receiver, aead) is False


def test_a_forged_header_is_rejected():
    # The header is the AEAD's AAD; rewriting the declared kind breaks the tag even though
    # the ciphertext is untouched. Byte 1 is VT, so this is an attacker trying to pass a
    # DATA frame off as a different kind.
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = bytearray(_data_packet(b"sensitive").get_secure_content(sender, aead))
    wire[1] ^= 0x01                             # DATA -> OK in the version+kind byte
    got = Packet_v3(addressing="sid")
    assert got.load_secure(bytes(wire), receiver, aead) is False


def test_a_forged_counter_is_rejected():
    # The anti-replay counter is in the AAD too, so it cannot be edited to slip a stale
    # frame past the replay window.
    sender, receiver = _sessions()
    aead = detect_aead()
    wire = bytearray(_data_packet(b"sensitive").get_secure_content(sender, aead))
    wire[3] ^= 0x0F                             # low byte of the counter
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
    c1 = struct.unpack(Packet_v3.SECURE_HEADER_FORMAT_SID_P2P, w1[:Packet_v3.SECURE_HEADER_SIZE_SID_P2P])[-1]
    c2 = struct.unpack(Packet_v3.SECURE_HEADER_FORMAT_SID_P2P, w2[:Packet_v3.SECURE_HEADER_SIZE_SID_P2P])[-1]
    assert c1 == 1 and c2 == 2


def test_the_two_directions_never_share_a_keystream():
    # The per-direction-prefix fix. Both ends' first frame uses counter 1, but the disjoint
    # send prefixes keep their (key, nonce) pairs distinct — so sealing the *same* plaintext
    # each way yields *different* wire bytes (no keystream reuse), and each frame opens only
    # at the peer. With a single shared prefix this test would fail (identical ciphertext).
    sender, receiver = _sessions()
    aead = detect_aead()
    plaintext = b"identical reading"

    wire_up = _data_packet(plaintext).get_secure_content(sender, aead)    # sender -> receiver, ctr 1
    wire_down = _data_packet(plaintext).get_secure_content(receiver, aead)  # receiver -> sender, ctr 1
    assert wire_up != wire_down                       # same plaintext + counter, disjoint nonces

    got_up = Packet_v3(addressing="sid")
    assert got_up.load_secure(wire_up, receiver, aead) is True and got_up.get_payload() == plaintext
    got_down = Packet_v3(addressing="sid")
    assert got_down.load_secure(wire_down, sender, aead) is True and got_down.get_payload() == plaintext

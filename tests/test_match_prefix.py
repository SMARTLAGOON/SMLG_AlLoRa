"""Unit — match specs expose their keyless wire prefix for the tunnel bridge.

The split Connector runs the wait-for-match loop at the radio (so a tunnel never ferries a
foreign frame across the slow link), which means the bridge gets the match key as opaque
*prefix bytes* it compares against a reply's head — no codec, no keys. `wire_prefix()` is that
serialization; a plain prefix compare on the bridge must reproduce the spec's own
`matches_wire`, for every addressing the protocol speaks.
"""
import struct

from AlLoRa.Codec import _MacMatchSpec, _SidMatchSpec, _DidMatchSpec


def _prefix_matches(prefix, wire):
    return len(wire) >= len(prefix) and wire[:len(prefix)] == prefix


def test_sid_prefix_is_the_sid_byte_and_reproduces_matches_wire():
    s = _SidMatchSpec(0x2a)
    assert s.wire_prefix() == bytes([0x2a])
    ours = bytes([0x2a, 0x00, 0x99])
    foreign = bytes([0x63, 0x00, 0x99])
    assert _prefix_matches(s.wire_prefix(), ours) == s.matches_wire(ours) is True
    assert _prefix_matches(s.wire_prefix(), foreign) == s.matches_wire(foreign) is False


def test_did_prefix_is_the_four_byte_token_and_reproduces_matches_wire():
    did = bytes([0xd9, 0x09, 0xf4, 0xeb])
    s = _DidMatchSpec(did)
    assert s.wire_prefix() == did
    ours = did + bytes([0x01, 0x02])
    foreign = bytes([0x00, 0x00, 0x00, 0x00, 0x01])
    assert _prefix_matches(s.wire_prefix(), ours) == s.matches_wire(ours) is True
    assert _prefix_matches(s.wire_prefix(), foreign) == s.matches_wire(foreign) is False


def test_mac_prefix_is_peer_then_me_and_reproduces_matches_wire():
    s = _MacMatchSpec("a1a1a1a1", "b2b2b2b2", short_mac=True)
    peer = struct.pack("I", int("a1a1a1a1", 16))
    me = struct.pack("I", int("b2b2b2b2", 16))
    assert s.wire_prefix() == peer + me
    ours = peer + me + b"\x01"
    foreign = me + peer + b"\x01"          # addresses reversed -> not our reply
    assert _prefix_matches(s.wire_prefix(), ours) == s.matches_wire(ours) is True
    assert _prefix_matches(s.wire_prefix(), foreign) == s.matches_wire(foreign) is False

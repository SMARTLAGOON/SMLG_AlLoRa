"""v2->v3 negotiation — the beacon bit + the version-decision policy.

First contact is a **v2-compatible poll carrying a v3-capable beacon in v2's spare bit 2**
(v2 never reads it, so there is no failed first attempt). A v3 peer echoes the bit ->
both know they're v3; a v2 peer doesn't -> stay plaintext v2. A pubkey-registered
(operational) peer is **pinned to v3** and never silently downgraded even if the echo is
lost/jammed (anti-downgrade).

This locks the two crypto-free halves of that mechanism: (1) the beacon rides bit 2 of a
real v2 frame without disturbing it, and (2) the decision table. Wiring the *runtime*
upgrade into the live transfer pairs with the secure handshake (increment 2), which is
the upgrade target.
"""
from AlLoRa.Packet import Packet
from AlLoRa.negotiation import negotiate_version, V2, V3

SRC = "a1a1a1a1"
DST = "b2b2b2b2"


def _v2_ok(beacon):
    p = Packet(mesh_mode=False, short_mac=True)
    p.set_source(SRC)
    p.set_destination(DST)
    p.set_ok()
    if beacon:
        p.set_v3_beacon(True)
    return p


# --- the beacon bit rides a v2 frame, backward-safe -------------------------

def test_beacon_bit_does_not_disturb_a_v2_frame():
    wire = _v2_ok(beacon=True).get_content()

    got = Packet(mesh_mode=False, short_mac=True)
    assert got.load(wire) is True
    # A v2 receiver still parses the command and every other flag exactly.
    assert got.get_command() == "OK"
    assert got.get_mesh() is False
    assert got.get_change_rf() is False
    assert got.get_hop() is False
    # ...and a v3 receiver can read the capability bit.
    assert got.get_v3_beacon() is True


def test_beacon_defaults_off():
    got = Packet(mesh_mode=False, short_mac=True)
    assert got.load(_v2_ok(beacon=False).get_content()) is True
    assert got.get_v3_beacon() is False


def test_beacon_uses_the_freed_spare_bit_2_only():
    # bit 2 was v2's single spare (contested 3 ways) — v3 frees it as the beacon.
    # Setting the beacon must flip exactly bit 2 of the flags byte and nothing else.
    with_beacon = bytearray(_v2_ok(beacon=True).get_content())
    without = bytearray(_v2_ok(beacon=False).get_content())
    flags_i = 8  # P2P short-MAC: src4 + dst4, flags byte at offset 8
    assert (without[flags_i] >> 2) & 1 == 0
    assert (with_beacon[flags_i] >> 2) & 1 == 1
    assert with_beacon[flags_i] ^ without[flags_i] == (1 << 2)  # only bit 2 differs


# --- the version-decision policy (anti-downgrade) ---------------------------

def test_unknown_peer_without_echo_defaults_to_v2():
    assert negotiate_version(registered_v3=False, beacon_echoed=False) == V2


def test_peer_that_echoes_the_beacon_is_v3():
    assert negotiate_version(registered_v3=False, beacon_echoed=True) == V3


def test_registered_peer_is_pinned_to_v3_even_if_echo_is_lost():
    # anti-downgrade: an operational (registered) node is never dropped to v2, even if
    # the echo was jammed/lost.
    assert negotiate_version(registered_v3=True, beacon_echoed=False) == V3
    assert negotiate_version(registered_v3=True, beacon_echoed=True) == V3

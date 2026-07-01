"""v2<->v3 version negotiation policy.

Kept as a pure decision so it is trivially testable and shared by both the Collector's
first-contact logic and the (later) runtime upgrade path. The mechanism it decides over:
a v3-capable node beacons in v2's freed spare bit 2 (`Packet.set_v3_beacon`); a v3 peer
echoes it. This function turns "did the peer echo?" + "is the peer registered?" into the
protocol version to speak.
"""

V2 = 2
V3 = 3


def negotiate_version(registered_v3, beacon_echoed):
    """Return the protocol version to use with a peer after a first-contact poll.

    - A pubkey-registered (operational) peer is **pinned to v3** — never silently
      downgraded, even if the echo is jammed/lost (anti-downgrade). This stops an
      attacker forcing a secure node back to plaintext.
    - Otherwise a peer that **echoes the v3 beacon** is v3.
    - Otherwise (a legacy peer, or genuine first contact) default to **v2** — the
      retro-compat path, so a v3 Gateway can still consume un-reflashed v2 field nodes.
    """
    if registered_v3:
        return V3
    return V3 if beacon_echoed else V2

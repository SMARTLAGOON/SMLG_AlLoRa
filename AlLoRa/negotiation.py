"""Decides whether to speak v2 or v3 with a peer. It only decides; it sends nothing.

A v3 node sets a beacon bit in v2's spare flag bit (`Packet.set_v3_beacon`), and a v3 peer echoes
it. The inputs are: did the peer echo it, and is the peer registered?
"""

V2 = 2
V3 = 3


def negotiate_version(registered_v3, beacon_echoed):
    """Return the protocol version to use with a peer after a first-contact poll.

    - A pubkey-registered (operational) peer is **pinned to v3**, never silently
      downgraded, even if the echo is jammed/lost (anti-downgrade). This stops an
      attacker forcing a secure node back to plaintext.
    - Otherwise a peer that **echoes the v3 beacon** is v3.
    - Otherwise (a legacy peer, or genuine first contact) default to **v2**, the
      retro-compat path, so a v3 Hub can still consume un-reflashed v2 field nodes.
    """
    if registered_v3:
        return V3
    return V3 if beacon_echoed else V2

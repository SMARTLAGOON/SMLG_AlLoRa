"""Codec — the seam that *speaks* a `Packet` on the wire.

A `Packet` is the typed wire unit — what a frame means (kind, sid, flags, payload). A `Codec`
is how that unit is encoded on the wire and read back off it. Framing used to be scattered:
the version choice lived on the `Connector` (`_new_response_packet` / `_response_matches`),
and the security posture lived as a parallel method-pair on `Packet_v3`
(`get_content`/`load` vs `get_secure_content`/`load_secure`). This folds all of that behind
one narrow interface so the transfer engine never branches on version or posture again:

    frame(packet)      -> wire bytes                (serialize; picks the open/secure body)
    deframe(wire)      -> packet | None             (parse; None = unparseable/corrupt/forged)
    match_spec(request)-> a spec whose .matches(reply) says "is this the reply to my request?"

There is one implementation per (version x posture) — a new version or security mode is a new
implementation, never a wider interface:

  * `V2Codec`      — legacy MAC-addressed frame + checksum (v2 predates secure mode).
  * `V3OpenCodec`  — v3 sid-addressed typed frame + 24-bit integrity, no crypto.
  * `V3SecureCodec`— v3 secure frame: the integrity trailer replaced by an AEAD-sealed
                     payload. This is the *only* crypto home; `Packet_v3.get_secure_content`
                     /`load_secure` are its private mechanism, not a public surface.

The secure codec resolves the per-peer `Session` from the `sid`, which is cleartext at wire
offset 0 in *both* open and secure frames — the send side by the outgoing packet's sid, the
receive side by the sid on the wire. So the engine calls `frame`/`deframe` identically for
open and secure; the posture never leaks up. The reply match is likewise keyless (v3: the
cleartext sid), which is what will let it move down to the radio later without keys.
"""
import struct

from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3


class _MacMatchSpec:
    """v2: a reply belongs to our request when the MACs mirror — it came *from* the peer we
    asked (`reply.src == request.dst`) and is addressed *to* us (`reply.dst == my_mac`).

    `matches` works on the parsed packet; `matches_wire` does the same check on the raw
    wire prefix (src bytes then dst bytes) so a keyless bridge can apply it at the radio."""

    def __init__(self, peer_mac, my_mac, short_mac=True):
        self._peer = peer_mac
        self._me = my_mac
        # The wire prefix for at-the-radio matching: src[0:n] then dst[n:2n]. Short MAC packs
        # each address as a 4-byte compressed int (the v2 header's `4s` fields).
        if short_mac:
            self._peer_bytes = struct.pack('I', int(peer_mac, 16))
            self._me_bytes = struct.pack('I', int(my_mac, 16))
        else:
            self._peer_bytes = peer_mac.encode()
            self._me_bytes = my_mac.encode()

    def matches(self, reply):
        return reply.get_source() == self._peer and reply.get_destination() == self._me

    def matches_wire(self, wire):
        n = len(self._peer_bytes)
        return (len(wire) >= 2 * n
                and wire[0:n] == self._peer_bytes
                and wire[n:2 * n] == self._me_bytes)


class _SidMatchSpec:
    """v3: a reply belongs to our request when they share a session id. Keyless and
    posture-independent (the sid is cleartext at offset 0 in open *and* secure frames), so
    the same spec matches either, and `matches_wire` runs the check on the wire prefix at
    the radio without keys."""

    def __init__(self, sid):
        self._sid = sid

    def matches(self, reply):
        return reply.get_session() == self._sid

    def matches_wire(self, wire):
        return len(wire) >= 1 and wire[0] == self._sid


class V2Codec:

    def __init__(self, mesh_mode=False, short_mac=False, my_mac="00000000"):
        self._mesh = mesh_mode
        self._short_mac = short_mac
        self._my_mac = my_mac

    def frame(self, packet):
        return packet.get_content()

    def deframe(self, wire):
        p = Packet(self._mesh, self._short_mac)
        try:
            return p if p.load(wire) else None
        except Exception:
            return None

    def match_spec(self, request):
        return _MacMatchSpec(request.get_destination(), self._my_mac, self._short_mac)


class V3OpenCodec:

    def __init__(self, mesh_mode=False, addressing="sid"):
        self._mesh = mesh_mode
        self._addressing = addressing

    def _new(self):
        return Packet_v3(mesh_mode=self._mesh, addressing=self._addressing)

    def frame(self, packet):
        return packet.get_content()

    def deframe(self, wire):
        p = self._new()
        try:
            return p if p.load(wire) else None
        except Exception:
            return None

    def match_spec(self, request):
        return _SidMatchSpec(request.get_session())


class V3SecureCodec:

    def __init__(self, session_resolver, aead, mesh_mode=False, addressing="sid"):
        self._resolve = session_resolver   # sid -> Session | None
        self._aead = aead
        self._mesh = mesh_mode
        self._addressing = addressing

    def _new(self):
        return Packet_v3(mesh_mode=self._mesh, addressing=self._addressing)

    def frame(self, packet):
        session = self._resolve(packet.get_session())
        if session is None:
            # Sealing a frame needs the peer's session; its absence is a wiring error, not a
            # routine outcome (graceful degradation to open is a posture decision the node
            # makes before choosing this codec, not something to paper over here).
            raise ValueError("no secure session for sid {}".format(packet.get_session()))
        return packet.get_secure_content(session, self._aead)

    def deframe(self, wire):
        if not wire:
            return None
        sid = wire[0]                      # cleartext, offset 0 (secure header is "!BBBH")
        session = self._resolve(sid)
        if session is None:
            return None                    # unknown session -> unauthenticatable -> reject
        p = self._new()
        try:
            return p if p.load_secure(wire, session, self._aead) else None
        except Exception:
            return None

    def match_spec(self, request):
        return _SidMatchSpec(request.get_session())


def build_codec(protocol_version=2, addressing="mac", mesh_mode=False, short_mac=False,
                my_mac="00000000", security_mode="open", session_resolver=None, aead=None):
    """Pick the codec for a node's negotiated version x posture.

    v2 has no posture. v3 is open unless a secure posture *and* a working AEAD backend *and* a
    session resolver are all present — a missing backend degrades to open (the node has
    already decided that is acceptable for its posture, per the secure-mode trust model).
    """
    if protocol_version >= 3:
        if (security_mode in ("secure", "strict")
                and session_resolver is not None and aead is not None):
            return V3SecureCodec(session_resolver, aead, mesh_mode, addressing)
        return V3OpenCodec(mesh_mode, addressing)
    return V2Codec(mesh_mode, short_mac, my_mac)

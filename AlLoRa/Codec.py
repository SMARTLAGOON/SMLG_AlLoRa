"""Codec: the seam that *speaks* a `Packet` on the wire.

A `Packet` is the typed wire unit, what a frame means (kind, sid, flags, payload). A `Codec`
is how that unit is encoded on the wire and read back off it. Framing used to be scattered:
the version choice lived on the `Connector` (`_new_response_packet` / `_response_matches`),
and the security posture lived as a parallel method-pair on `Packet_v3`
(`get_content`/`load` vs `get_secure_content`/`load_secure`). This folds all of that behind
one narrow interface so the transfer engine never branches on version or posture again:

    frame(packet)      -> wire bytes                (serialize; picks the open/secure body)
    deframe(wire)      -> packet | None             (parse; None = unparseable/corrupt/forged)
    match_spec(request)-> a spec whose .matches(reply) says "is this the reply to my request?"
    payload_overhead() -> bytes a frame costs around its payload (what caps the chunk size)

`payload_overhead` is here because the cost is a property of the framing and nothing else:
v2 spends 12 bytes on two MAC addresses, v3 addresses by session id in 6, and secure trades
the integrity trailer for a sealed header plus a tag. A node that derived it any other way
would be re-deriving this dispatch, and would drift from it.

There is one implementation per (version x posture). A new version or security mode is a new
implementation, never a wider interface:

  * `V2Codec`: legacy MAC-addressed frame + checksum (v2 predates secure mode).
  * `V3OpenCodec`: v3 sid-addressed typed frame + 24-bit integrity, no crypto.
  * `V3SecureCodec`: v3 secure frame: the integrity trailer replaced by an AEAD-sealed
                     payload. This is the *only* crypto home; `Packet_v3.get_secure_content`
                     /`load_secure` are its private mechanism, not a public surface.

The secure codec resolves the per-peer `Session` from the `sid`, which is cleartext at wire
offset 0 in *both* open and secure frames, the send side by the outgoing packet's sid, the
receive side by the sid on the wire. So the engine calls `frame`/`deframe` identically for
open and secure; the posture never leaks up. The reply match is likewise keyless (v3: the
cleartext sid), which is what will let it move down to the radio later without keys.
"""
import struct

from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3


class _MacMatchSpec:
    """v2: a reply belongs to our request when the MACs mirror: it came *from* the peer we
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

    def wire_prefix(self):
        # The contiguous offset-0 bytes a keyless bridge compares (src then dst); `matches_wire`
        # is exactly "wire starts with this". Sent down so the tunnel matches at the radio.
        return self._peer_bytes + self._me_bytes


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

    def wire_prefix(self):
        # The cleartext sid byte at offset 0, the keyless prefix the tunnel bridge matches on.
        return bytes([self._sid])


class _DidMatchSpec:
    """v3 first contact: a reply belongs to our request when it carries the same device_id[:4]
    token. One 4-byte address, symmetric in both directions (the Collector polls a Source's
    did; the Source answers under it), cleartext at wire offset 0, so like the sid spec it is
    keyless and `matches_wire` runs at the radio without touching a key."""

    def __init__(self, did):
        self._did = bytes(did)

    def matches(self, reply):
        return reply.get_did() == self._did

    def matches_wire(self, wire):
        n = len(self._did)
        return len(wire) >= n and wire[:n] == self._did

    def wire_prefix(self):
        # The device_id[:4] token at offset 0, keyless, so the tunnel bridge matches it at
        # the radio without touching a session.
        return self._did


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

    def payload_overhead(self):
        # Built rather than looked up so the number cannot drift from the header table.
        return Packet(self._mesh, self._short_mac).HEADER_SIZE


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
        if request.addressing == "did":
            return _DidMatchSpec(request.get_did())
        return _SidMatchSpec(request.get_session())

    def payload_overhead(self):
        # HEADER_SIZE already includes the 24-bit integrity trailer, so it is the whole cost
        # of a frame around its payload.
        return self._new().HEADER_SIZE


class V3SecureCodec:
    """Secure posture, but *hybrid*: it speaks sid-addressed data frames sealed, and
    MAC-addressed first-contact/handshake CTRL frames open (they carry public keys and no
    session exists yet). A node has to hold both because first contact bootstraps the very
    session the data path needs, and a Gateway does it with different Sources over its
    lifetime. Framing is routed by the packet's own addressing; parsing try-parses (the wire
    format has both frame shapes but no marker to tell them apart), using the AEAD tag / the
    24-bit integrity already on the wire as the validity check, no new wire field.

    P2P only, and unlike the other two codecs it takes no `mesh_mode`: the sealed header has
    no sequence number, so AlLoRa's own flooding mesh has no secure frame shape to travel in.
    The intended route to secure multi-hop is to ride a Meshtastic mesh instead (that connector
    is not built yet), and it needs nothing from this class: Meshtastic carries its routing in
    its own frame, so an AlLoRa frame inside one stays P2P exactly as it is here."""

    def __init__(self, session_resolver, aead, addressing="sid", my_mac="00000000"):
        self._resolve = session_resolver   # sid -> Session | None
        self._aead = aead
        self._addressing = addressing
        self._my_mac = my_mac

    def _new(self, addressing):
        # No mesh_mode to pass: unlike the other two codecs this one takes none, because it
        # could not honour one. Every frame it builds is P2P, sealed or handshake alike.
        return Packet_v3(addressing=addressing)

    def frame(self, packet):
        # First-contact handshake frames go on the wire open (public keys, nothing secret),
        # whether device_id-addressed (v3) or MAC-addressed (the retiring v2-compat shape);
        # established sid-addressed frames are sealed.
        if packet.addressing in ("did", "mac"):
            return packet.get_content()
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
        # Common case first: a secure sid-addressed frame. A verifying 4-byte AEAD tag is a
        # strong "yes, secure": a handshake frame won't have a valid tag under a session key.
        session = self._resolve(wire[0])           # wire[0] = sid (or, for a MAC frame, a MAC byte)
        if session is not None:
            p = self._new("sid")
            try:
                if p.load_secure(wire, session, self._aead):
                    return p
            except Exception:
                pass
        # Otherwise an open MAC-addressed handshake CTRL frame, accepted only if its 24-bit
        # integrity checks, it is a CTRL frame, and it is addressed to me. (Retiring v2-compat
        # shape; the v3 first-contact frame is device_id-addressed, handled next.)
        h = self._new("mac")
        try:
            if (h.load(wire) and h.get_command() == Packet_v3.CTRL
                    and h.get_destination() == self._my_mac):
                return h
        except Exception:
            pass
        # Or an open device_id-addressed handshake CTRL frame. The did token is the *Source's*
        # in both directions, so a Collector serving many Sources can't tell "mine" from
        # "theirs" here. That decision moves to is_for_me (responder) / match_spec (initiator);
        # deframe only vouches for integrity + that it is a CTRL frame.
        d = self._new("did")
        try:
            if d.load(wire) and d.get_command() == Packet_v3.CTRL:
                return d
        except Exception:
            pass
        return None

    def match_spec(self, request):
        # A handshake round matches by its first-contact token (device_id[:4], or the retiring
        # MAC-mirror); an established round matches by sid.
        if request.addressing == "did":
            return _DidMatchSpec(request.get_did())
        if request.addressing == "mac":
            return _MacMatchSpec(request.get_destination(), self._my_mac, short_mac=True)
        return _SidMatchSpec(request.get_session())

    def payload_overhead(self):
        # The sealed sid-addressed shape, because that is what carries chunks: the open
        # first-contact frames this codec also speaks are handshakes, whose payloads are keys
        # of a fixed size, not file data. Sealed cost is the authenticated header plus the tag
        # (which replaces open mode's integrity trailer).
        return Packet_v3.SECURE_HEADER_SIZE_SID_P2P + Packet_v3.SECURE_TAG_LEN


def build_codec(protocol_version=2, addressing="mac", mesh_mode=False, short_mac=False,
                my_mac="00000000", security_mode="open", session_resolver=None, aead=None):
    """Pick the codec for a node's negotiated version x posture.

    v2 has no posture. v3 is open unless a secure posture *and* a working AEAD backend *and* a
    session resolver are all present. A missing backend degrades to open (the node has
    already decided that is acceptable for its posture, per the secure-mode trust model).
    """
    if protocol_version >= 3:
        if (security_mode in ("secure", "strict")
                and session_resolver is not None and aead is not None):
            if mesh_mode:
                # Secure framing is sid-addressed P2P only: the sealed header carries no seq
                # field, so this node would frame P2P while believing it forwards, and fail in
                # a shape that looks like a radio fault. Refuse at startup, where a
                # misconfiguration belongs, rather than at the first exchange. Only AlLoRa's
                # own flooding mesh is at stake: riding a Meshtastic mesh keeps this frame P2P
                # (the routing lives in the Meshtastic frame), so it needs no mesh header here.
                raise ValueError(
                    "secure mode does not support AlLoRa's own flooding mesh: the sealed "
                    "header has no sequence number, so this node would frame point to point "
                    "while believing it forwards. Clear mesh_mode to run secure point to "
                    "point, or use open mode if you need the flooding mesh.")
            return V3SecureCodec(session_resolver, aead, addressing, my_mac)
        return V3OpenCodec(mesh_mode, addressing)
    return V2Codec(mesh_mode, short_mac, my_mac)

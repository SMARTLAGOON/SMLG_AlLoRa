"""The v3 wire unit — a typed, versioned Packet.

v2's `Packet` fused the command into two bits of a flags byte, addressed every frame
with two 4-byte MACs, and stored a 12-bit hex "checksum" in a 3-byte field. v3 fixes all
three at once, without fattening the header:

  * a **version nibble** (0x3) makes the format self-describing and extensible (v4+ headroom),
    with a **typed-kind nibble** replacing the 2-bit command;
  * **session-id addressing** collapses the two MACs to one byte once a session exists
    (MACs survive only in the handshake / mesh, where there's no session or a relay needs
    the real destination);
  * the integrity trailer becomes a **real 24-bit** `sha256(payload)` digest.

This is the *codec only* — the wire structure, zero crypto (the "open" security posture).
Secure mode swaps the integrity trailer for an AEAD tag and adds an anti-replay counter;
the kind/flag/addressing machinery here is shared by both. v2 `Packet` is left untouched
so a v3 node can still fall back to a legacy peer during version negotiation.

Header layouts (P2P shown; mesh inserts a 2-byte `seq` before `integ`):

    MAC-addressed  [src4][dst4][VT1][FL1][integ3]   = 13 B   (handshake / first contact)
    sid-addressed  [sid1][VT1][FL1][integ3]         =  6 B   (established session)

    VT   = version(4b)=0x3 | kind(4b)
    FL   = mesh|sleep|hop|debug_hops|role_token|auth|cfg_epoch|spare
    integ = sha256(payload)[:3]
"""
import struct
import hashlib
from math import ceil

from AlLoRa.utils.debug_utils import print


class Packet_v3:

    VERSION = 0x3

    # Header formats. `!` = network byte order; `4s`/`Ns` carry raw bytes we pack ourselves.
    #                              src dst VT FL      seq integ
    HEADER_FORMAT_MAC_P2P   = "!4s4sBB3s"     # 13 B
    HEADER_FORMAT_SID_P2P   = "!BBB3s"        #  6 B
    HEADER_FORMAT_MAC_MESH  = "!4s4sBB2s3s"   # 15 B
    HEADER_FORMAT_SID_MESH  = "!BBB2s3s"      #  8 B
    HEADER_SIZE_MAC_P2P  = 13
    HEADER_SIZE_SID_P2P  = 6
    HEADER_SIZE_MAC_MESH = 15
    HEADER_SIZE_SID_MESH = 8

    # Typed packet kinds (VT low nibble). DATA/OK/CHUNK/METADATA are the transfer core;
    # GRANT (role-swap), CTRL (control commands), ACKMAP (selective-repeat) are reserved
    # here and filled in by later increments — but they already have wire codes so the
    # format never has to break to add them.
    DATA = "DATA"
    OK = "OK"
    CHUNK = "CHUNK"
    METADATA = "METADATA"
    GRANT = "GRANT"
    CTRL = "CTRL"
    ACKMAP = "ACKMAP"
    KIND_CODES = {DATA: 0x0, OK: 0x1, CHUNK: 0x2, METADATA: 0x3,
                  GRANT: 0x4, CTRL: 0x5, ACKMAP: 0x6}
    KIND_NAMES = {v: k for k, v in KIND_CODES.items()}

    # FL flag-byte bit positions.
    _FL_MESH = 0
    _FL_SLEEP = 1
    _FL_HOP = 2
    _FL_DEBUG_HOPS = 3
    _FL_ROLE_TOKEN = 4
    _FL_AUTH = 5
    _FL_CFG_EPOCH = 6

    @staticmethod
    def check_command(command):
        return command in Packet_v3.KIND_CODES

    def __init__(self, mesh_mode=False, addressing="sid", short_mac=True):
        if addressing not in ("sid", "mac"):
            raise ValueError("addressing must be 'sid' or 'mac', got {}".format(addressing))
        self.mesh_mode = mesh_mode
        self.addressing = addressing
        # short_mac is accepted for signature symmetry with v2; v3 MAC addressing is
        # always the 4-byte short MAC (decision E), so long MAC has no v3 header.

        if addressing == "mac":
            self.HEADER_FORMAT = self.HEADER_FORMAT_MAC_MESH if mesh_mode else self.HEADER_FORMAT_MAC_P2P
            self.HEADER_SIZE = self.HEADER_SIZE_MAC_MESH if mesh_mode else self.HEADER_SIZE_MAC_P2P
        else:
            self.HEADER_FORMAT = self.HEADER_FORMAT_SID_MESH if mesh_mode else self.HEADER_FORMAT_SID_P2P
            self.HEADER_SIZE = self.HEADER_SIZE_SID_MESH if mesh_mode else self.HEADER_SIZE_SID_P2P

        self.src = b"\x00\x00\x00\x00"   # 4-byte compressed short MAC (mac addressing)
        self.dst = b"\x00\x00\x00\x00"
        self.sid = 0                     # 1-byte session id (sid addressing)
        self.seq = 0                     # 2-byte mesh sequence number

        self.kind = None                 # one of the KIND_* names
        self.payload = b""
        self.check = None                # True once load() verifies the integrity trailer
        self.content = None

        # FL flags
        self.mesh = False
        self.sleep = True
        self.hop = False
        self.debug_hops = False
        self.role_token = False
        self.auth = False
        self.cfg_epoch = False

    def __repr__(self):
        who = "sid={}".format(self.sid) if self.addressing == "sid" \
            else "src={} dst={}".format(self.get_source(), self.get_destination())
        return "Packet_v3(v{}, {}, kind={}, {}, payload={}, check={})".format(
            self.VERSION, self.addressing, self.kind, who, self.payload, self.check)

    # --- addressing ---------------------------------------------------------

    def _mac_compress(self, mac):
        return struct.pack("I", int(mac, 16))

    def _mac_decompress(self, raw):
        return "{:08x}".format(struct.unpack("I", raw)[0])

    def set_source(self, source):
        self.src = self._mac_compress(source)

    def get_source(self):
        return self._mac_decompress(self.src)

    def set_destination(self, destination):
        self.dst = self._mac_compress(destination)

    def get_destination(self):
        return self._mac_decompress(self.dst)

    def set_session(self, sid):
        if not 0 <= sid <= 255:
            raise ValueError("session id must fit one byte (0..255)")
        self.sid = sid

    def get_session(self):
        return self.sid

    def set_seq(self, seq):
        if 0 <= seq <= 65535:
            self.seq = seq

    def get_seq(self):
        return self.seq

    # --- kind / version -----------------------------------------------------

    def set_kind(self, kind):
        if kind not in self.KIND_CODES:
            raise ValueError("unknown kind {}".format(kind))
        self.kind = kind

    def get_command(self):
        return self.kind

    def get_version(self):
        return self.VERSION

    # --- typed payload accessors -------------------------------------------

    def set_payload(self, payload):
        self.payload = payload

    def get_payload(self):
        return self.payload

    def set_data(self, chunk):
        self.kind = self.DATA
        self.payload = bytes(chunk)

    def set_ok(self):
        self.kind = self.OK
        self.payload = b""

    def ask_metadata(self):
        self.kind = self.METADATA
        self.payload = b""

    def set_metadata(self, chunk_size, total_len, filename):
        # Typed METADATA: the receiver needs chunk_size to place positioned writes, and
        # total_len to know the last chunk's exact length — neither of which v2 sent (it
        # carried only chunk_count + filename, so v2 leaned on both ends matching config).
        self.kind = self.METADATA
        self.payload = (struct.pack("<H", chunk_size)
                        + struct.pack("<I", total_len)
                        + filename.encode())

    def get_metadata(self):
        if self.kind != self.METADATA:
            return None
        try:
            chunk_size = struct.unpack("<H", self.payload[:2])[0]
            total_len = struct.unpack("<I", self.payload[2:6])[0]
            filename = self.payload[6:].decode()
            length = ceil(total_len / chunk_size) if chunk_size else 0
            return {"CHUNK_SIZE": chunk_size, "TOTAL_LEN": total_len,
                    "FILENAME": filename, "LENGTH": length}
        except Exception:
            return None

    def ask_data(self, chunk_index):
        # Binary 2-byte index (0..65535), vs v2's ASCII decimal (which bloated with the
        # index and could not be length-bounded).
        self.kind = self.CHUNK
        self.payload = struct.pack("<H", chunk_index)

    def get_chunk_index(self):
        if self.kind != self.CHUNK:
            return None
        return struct.unpack("<H", self.payload[:2])[0]

    # --- FL flags -----------------------------------------------------------

    def set_role_token(self, on=True):
        self.role_token = on

    def get_role_token(self):
        return self.role_token

    def enable_mesh(self):
        self.mesh = True

    def disable_mesh(self):
        self.mesh = False

    def get_mesh(self):
        return self.mesh

    def enable_hop(self):
        self.hop = True

    def get_hop(self):
        return self.hop

    def enable_sleep(self):
        self.sleep = True

    def disable_sleep(self):
        self.sleep = False

    def get_sleep(self):
        return self.sleep

    def enable_debug_hops(self):
        self.debug_hops = True

    def get_debug_hops(self):
        return self.debug_hops

    # --- integrity ----------------------------------------------------------

    def get_integrity(self, payload):
        # A real 24-bit digest. v2 stored hexlify(sha256(payload))[-3:] here -> 3 hex
        # characters = 12 bits of entropy in a 3-byte field. This uses the raw digest.
        return hashlib.sha256(payload).digest()[:3]

    # --- framing ------------------------------------------------------------

    def _pack_vt(self):
        return (self.VERSION << 4) | self.KIND_CODES[self.kind]

    def _pack_fl(self):
        fl = 0
        if self.mesh:        fl |= (1 << self._FL_MESH)
        if self.sleep:       fl |= (1 << self._FL_SLEEP)
        if self.hop:         fl |= (1 << self._FL_HOP)
        if self.debug_hops:  fl |= (1 << self._FL_DEBUG_HOPS)
        if self.role_token:  fl |= (1 << self._FL_ROLE_TOKEN)
        if self.auth:        fl |= (1 << self._FL_AUTH)
        if self.cfg_epoch:   fl |= (1 << self._FL_CFG_EPOCH)
        return fl

    def _unpack_fl(self, fl):
        self.mesh        = bool(fl & (1 << self._FL_MESH))
        self.sleep       = bool(fl & (1 << self._FL_SLEEP))
        self.hop         = bool(fl & (1 << self._FL_HOP))
        self.debug_hops  = bool(fl & (1 << self._FL_DEBUG_HOPS))
        self.role_token  = bool(fl & (1 << self._FL_ROLE_TOKEN))
        self.auth        = bool(fl & (1 << self._FL_AUTH))
        self.cfg_epoch   = bool(fl & (1 << self._FL_CFG_EPOCH))

    def build_header(self):
        vt = self._pack_vt()
        fl = self._pack_fl()
        integ = self.get_integrity(self.payload)
        seq = struct.pack("<H", self.seq)
        if self.addressing == "mac":
            if self.mesh_mode:
                return struct.pack(self.HEADER_FORMAT, self.src, self.dst, vt, fl, seq, integ)
            return struct.pack(self.HEADER_FORMAT, self.src, self.dst, vt, fl, integ)
        else:
            if self.mesh_mode:
                return struct.pack(self.HEADER_FORMAT, self.sid, vt, fl, seq, integ)
            return struct.pack(self.HEADER_FORMAT, self.sid, vt, fl, integ)

    def close_packet(self):
        if self.kind not in self.KIND_CODES:
            return
        self.content = self.build_header() + self.payload

    def get_content(self):
        # Always rebuild: callers mutate payload/flags after construction, and the frame
        # is cheap to reframe. (v2 caches; v3 keeps it simple and stale-proof.)
        self.close_packet()
        return self.content

    def get_length(self):
        return self.HEADER_SIZE + len(self.payload)

    def load(self, packet):
        header = packet[:self.HEADER_SIZE]
        self.payload = packet[self.HEADER_SIZE:]

        try:
            if self.addressing == "mac":
                if self.mesh_mode:
                    self.src, self.dst, vt, fl, seq, integ = struct.unpack(self.HEADER_FORMAT, header)
                    self.seq = struct.unpack("<H", seq)[0]
                else:
                    self.src, self.dst, vt, fl, integ = struct.unpack(self.HEADER_FORMAT, header)
            else:
                if self.mesh_mode:
                    self.sid, vt, fl, seq, integ = struct.unpack(self.HEADER_FORMAT, header)
                    self.seq = struct.unpack("<H", seq)[0]
                else:
                    self.sid, vt, fl, integ = struct.unpack(self.HEADER_FORMAT, header)
        except Exception:
            self.check = False
            return False

        version = vt >> 4
        kind_code = vt & 0x0F
        self._unpack_fl(fl)

        if version != self.VERSION or kind_code not in self.KIND_NAMES:
            self.check = False
            return False

        self.kind = self.KIND_NAMES[kind_code]
        self.check = integ == self.get_integrity(self.payload)
        if self.check:
            self.content = packet
        return self.check

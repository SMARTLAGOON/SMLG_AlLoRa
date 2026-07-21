"""The v3 wire unit: a typed, versioned Packet.

v2's `Packet` fused the command into two bits of a flags byte, addressed every frame
with two 4-byte MACs, and stored a 12-bit hex "checksum" in a 3-byte field. v3 fixes all
three at once, without fattening the header:

  * a **version nibble** (0x3) makes the format self-describing and extensible (v4+ headroom),
    with a **typed-kind nibble** replacing the 2-bit command;
  * **session-id addressing** collapses the two MACs to one byte once a session exists
    (MACs survive only in the handshake / mesh, where there's no session or a relay needs
    the real destination);
  * the integrity trailer becomes a **real 24-bit** `sha256(payload)` digest.

This is the *codec only*, the wire structure, zero crypto (the "open" security posture).
Secure mode swaps the integrity trailer for an AEAD tag and adds an anti-replay counter;
the kind/flag/addressing machinery here is shared by both. v2 `Packet` is left untouched
so a v3 node can still fall back to a legacy peer during version negotiation.

Header layouts (P2P shown; mesh inserts a 2-byte `seq` before `integ`):

    MAC-addressed  [src4][dst4][VT1][FL1][integ3]   = 13 B   (v2-compat first contact)
    did-addressed  [did4][VT1][FL1][integ3]         =  9 B   (v3 first contact, no MAC on wire)
    sid-addressed  [sid1][VT1][FL1][integ3]         =  6 B   (established session)

    VT   = version(4b)=0x3 | kind(4b)
    FL   = mesh|sleep|hop|debug_hops|role_token|auth|cfg_epoch|spare
    integ = sha256(payload)[:3]

The did token is device_id[:4], one 4-byte identity address, the same in both directions
(the Collector polls it, the Source answers under it), so it matches at wire offset 0 exactly
like the sid. It replaces the two-MAC handshake header once a node is registered by device_id
rather than by MAC.
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
    HEADER_FORMAT_DID_P2P   = "!4sBB3s"       #  9 B
    HEADER_FORMAT_SID_P2P   = "!BBB3s"        #  6 B
    HEADER_FORMAT_MAC_MESH  = "!4s4sBB2s3s"   # 15 B
    HEADER_FORMAT_DID_MESH  = "!4sBB2s3s"     # 11 B
    HEADER_FORMAT_SID_MESH  = "!BBB2s3s"      #  8 B
    HEADER_SIZE_MAC_P2P  = 13
    HEADER_SIZE_DID_P2P  = 9
    HEADER_SIZE_SID_P2P  = 6
    HEADER_SIZE_MAC_MESH = 15
    HEADER_SIZE_DID_MESH = 11
    HEADER_SIZE_SID_MESH = 8

    # Secure-mode serialization. A secure frame replaces the open-mode 24-bit integrity
    # trailer with an AEAD-sealed payload: an authenticated header [sid|VT|FL|counter2],
    # then the AES-CTR ciphertext, then a 4-byte tag. The header is bound as the AEAD's AAD
    # so it can't be forged; the counter is the anti-replay token. sid-addressed P2P only for
    # now (mesh secure is a later add). This exact byte layout + the nonce assembly are a
    # defensible provisional default, deliberately left to a crypto-review pass, not frozen.
    SECURE_HEADER_FORMAT_SID_P2P = "!BBBH"   # sid, VT, FL, counter(2) = 5 B
    SECURE_HEADER_SIZE_SID_P2P = 5
    SECURE_TAG_LEN = 4
    _SECURE_ENC_KEY_LEN = 16                  # session.key = enc(16) || mac(16)

    # Typed packet kinds (VT low nibble). DATA/OK/CHUNK/METADATA are the transfer core;
    # GRANT (role-swap), CTRL (control commands), ACKMAP (selective-repeat) are reserved
    # here and filled in by later increments, but they already have wire codes so the
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
        if addressing not in ("sid", "mac", "did"):
            raise ValueError("addressing must be 'sid', 'mac' or 'did', got {}".format(addressing))
        self.mesh_mode = mesh_mode
        self.addressing = addressing
        # short_mac is accepted for signature symmetry with v2; v3 MAC addressing is
        # always the 4-byte short MAC (decision E), so long MAC has no v3 header.

        if addressing == "mac":
            self.HEADER_FORMAT = self.HEADER_FORMAT_MAC_MESH if mesh_mode else self.HEADER_FORMAT_MAC_P2P
            self.HEADER_SIZE = self.HEADER_SIZE_MAC_MESH if mesh_mode else self.HEADER_SIZE_MAC_P2P
        elif addressing == "did":
            self.HEADER_FORMAT = self.HEADER_FORMAT_DID_MESH if mesh_mode else self.HEADER_FORMAT_DID_P2P
            self.HEADER_SIZE = self.HEADER_SIZE_DID_MESH if mesh_mode else self.HEADER_SIZE_DID_P2P
        else:
            self.HEADER_FORMAT = self.HEADER_FORMAT_SID_MESH if mesh_mode else self.HEADER_FORMAT_SID_P2P
            self.HEADER_SIZE = self.HEADER_SIZE_SID_MESH if mesh_mode else self.HEADER_SIZE_SID_P2P

        self.src = b"\x00\x00\x00\x00"   # 4-byte compressed short MAC (mac addressing)
        self.dst = b"\x00\x00\x00\x00"
        self.did = b"\x00\x00\x00\x00"   # 4-byte device_id[:4] token (did addressing)
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
        if self.addressing == "sid":
            who = "sid={}".format(self.sid)
        elif self.addressing == "did":
            who = "did={}".format(self.did.hex())
        else:
            who = "src={} dst={}".format(self.get_source(), self.get_destination())
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

    def set_did(self, did):
        # device_id[:4]: the 4-byte first-contact address. Stored raw; whoever builds the
        # packet slices the fingerprint (or the registered token) to 4 bytes.
        did = bytes(did)
        if len(did) != 4:
            raise ValueError("did token must be exactly 4 bytes, got {}".format(len(did)))
        self.did = did

    def get_did(self):
        return self.did

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
        # total_len to know the last chunk's exact length, neither of which v2 sent (it
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

    def set_grant(self, swap_id):
        # The Hub delegating the drive role. The whole payload is a 1-byte rolling
        # counter, so the Edge can ignore a stale or duplicate GRANT by comparing it.
        if not 0 <= swap_id <= 255:
            raise ValueError("swap_id must fit one byte (0..255)")
        self.kind = self.GRANT
        self.payload = bytes([swap_id])

    def get_swap_id(self):
        if self.kind != self.GRANT or not self.payload:
            return None
        return self.payload[0]

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
        elif self.addressing == "did":
            if self.mesh_mode:
                return struct.pack(self.HEADER_FORMAT, self.did, vt, fl, seq, integ)
            return struct.pack(self.HEADER_FORMAT, self.did, vt, fl, integ)
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
            elif self.addressing == "did":
                if self.mesh_mode:
                    self.did, vt, fl, seq, integ = struct.unpack(self.HEADER_FORMAT, header)
                    self.seq = struct.unpack("<H", seq)[0]
                else:
                    self.did, vt, fl, integ = struct.unpack(self.HEADER_FORMAT, header)
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

    # --- secure framing (parallel to get_content/load; open-mode path above is untouched) --

    def get_secure_content(self, session, aead):
        """Serialize as a secure frame: an authenticated header + the AEAD-sealed payload.

        The header (sid, version+kind, flags, a fresh monotonic counter) is bound as the
        AEAD's AAD so it cannot be forged; the payload is encrypted and tagged. Consumes one
        send counter from the session, so it must be called exactly once per transmitted
        frame.
        """
        counter = session.next_counter()
        header = struct.pack(self.SECURE_HEADER_FORMAT_SID_P2P,
                             self.sid, self._pack_vt(), self._pack_fl(), counter)
        # Seal under my send-direction prefix (the peer opens with its matching recv prefix).
        nonce = session.send_nonce_prefix + struct.pack("!H", counter)   # prefix || 2-B counter
        enc_key = session.key[:self._SECURE_ENC_KEY_LEN]
        mac_key = session.key[self._SECURE_ENC_KEY_LEN:]
        sealed = aead.seal(enc_key, mac_key, nonce, header, self.payload)  # ciphertext || tag
        self.content = header + sealed
        return self.content

    def load_secure(self, wire, session, aead):
        """Parse, authenticate, decrypt and replay-check a secure frame into this packet.

        Returns True only if the tag verifies (header + payload untampered) *and* the frame
        is fresh (not a replay). On any failure nothing is decrypted into the packet.
        """
        if len(wire) < self.SECURE_HEADER_SIZE_SID_P2P + self.SECURE_TAG_LEN:
            self.check = False
            return False
        header = wire[:self.SECURE_HEADER_SIZE_SID_P2P]
        try:
            sid, vt, fl, counter = struct.unpack(self.SECURE_HEADER_FORMAT_SID_P2P, header)
        except Exception:
            self.check = False
            return False

        version = vt >> 4
        kind_code = vt & 0x0F
        if version != self.VERSION or kind_code not in self.KIND_NAMES:
            self.check = False
            return False

        # Open under my receive-direction prefix (= the peer's send prefix); the disjoint
        # prefixes are what stop the two directions from sharing a (key, nonce).
        nonce = session.recv_nonce_prefix + struct.pack("!H", counter)
        enc_key = session.key[:self._SECURE_ENC_KEY_LEN]
        mac_key = session.key[self._SECURE_ENC_KEY_LEN:]
        plaintext = aead.open(enc_key, mac_key, nonce, header,
                              wire[self.SECURE_HEADER_SIZE_SID_P2P:])
        if plaintext is None:
            self.check = False          # authentication failed -> reject, decrypt nothing
            return False
        if not session.accept(counter):
            self.check = False          # replay (or too old) -> reject
            return False

        self.sid = sid
        self.kind = self.KIND_NAMES[kind_code]
        self._unpack_fl(fl)
        self.payload = plaintext
        self.content = wire
        self.check = True
        return True

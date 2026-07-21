from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.time_utils import current_time_ms as time, sleep
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.json_utils import json

from os import urandom
from json import loads, dumps
    
class Node:

    def __init__(self, connector: Connector, config_file):
        self.config_file = config_file
        self.open_backup()
        self.connector = connector

        self.LAST_IDS = list()              # IDs from my mesagges
        self.LAST_SEEN_IDS = list()         # IDs from others
        self.MAX_IDS_CACHED = 30            # Max number of IDs saved

        self.sf_trial = None

        self.subscribers = []
        self.status = {}

        self.config_connector()

        self.status["Freq"] = self.connector.frequency
        self.status["SF"] = self.connector.sf
        self.status["BW"] = self.connector.bw
        self.status["CR"] = self.connector.cr
        self.status["TX_P"] = self.connector.tx_power

        self.status["Status"] = "WAIT"  # Status of the requester
        self.status["RSSI"] = "-" # Signal strength
        self.status["SNR"] = "-"  # Signal to Noise Ratio

        self.status["Chunk"] = "-"  # Chunk being received/sent
        self.status["File"] = "-"   # File name being received/sent
        self.status["PSizeS"] = "-" # Packet Size Sent
        self.status["PSizeR"] = "-" # Packet Size Received
        self.status["Retransmission"] = 0   # Number of retransmissions
        self.status["TimePS"] = "-"    # Time to send packet
        self.status["TimePR"] = "-"    # Waiting time for response
        self.status["TimeBtw"] = "-"  # Time between reply
        self.status["CorruptedPackets"] = 0  # Number of corrupted packets

        self.session_store = None
        self.aead = None
        self.static_priv = None     # the responder's long-lived ECDH key (built lazily)
        self._hs_state = None       # the initiator's ephemeral key, held between handshake rounds

        # Long-term identity (secure): the fingerprint of this key is the device_id. Distinct
        # from the per-session ephemeral used in the ECDH: the ephemeral rotates every session,
        # so it can't be a stable identity. Loaded on demand (never in open mode).
        self.identity_priv = None
        self.identity_pub = None
        self.device_id = None

        if self.security_mode == 'secure':
            self._enable_secure()

        # The 1-byte sid is identity-derived by default, config-overridable. Resolved here (not
        # in open_backup) because it needs the MAC (open) or the device_id (secure), both known
        # only after config_connector / _enable_secure.
        self.session_id = self._resolve_session_id()

    def _enable_secure(self):
        # Custody of secure Sessions (per-peer, keyed by sid) + the per-frame AEAD backend.
        # Imported lazily so an open-mode node never pulls in the crypto modules.
        from AlLoRa.Security.Session_store import RAM_session_store
        from AlLoRa.Security.AEAD import detect_aead
        self.session_store = RAM_session_store()
        self.aead = detect_aead()
        # A secure node always has a device_id: it is its wire identity (first-contact address
        # + sid seed), needed whether or not the sid is config-overridden.
        self._ensure_identity()
        if self.aead is not None:
            self.connector.set_secure(self.session_store.get, self.aead)
            return
        # No crypto backend. A configured-secure node must NOT silently run plaintext: the
        # operator believes the link is protected, so a silent degrade is the worst outcome.
        # It halts loudly, unless an explicit opt-in (tests / bring-up only) permits open.
        from AlLoRa.Security.AEAD import unavailable_reason
        reason = unavailable_reason()
        if self.config.get('allow_insecure_fallback', False):
            print("WARNING: secure mode requested but no AEAD backend, running OPEN (insecure),",
                  "because allow_insecure_fallback is set:", reason)
            return
        raise RuntimeError(
            "secure mode requires a crypto (AEAD) backend, none available: {}. Flash the AlLoRa "
            "firmware (native CTR), or set allow_insecure_fallback for an explicit insecure run "
            "(tests/bring-up only).".format(reason))

    def _ensure_identity(self):
        # Load (or, on first boot, generate + persist) this node's long-term identity keypair
        # and its device_id. Persisting keeps the device_id stable across reboots so the
        # operator's registration stays valid; without a configured identity_file the key is
        # RAM-only (fine for tests, but the device_id then changes each boot).
        if self.device_id is not None:
            return
        from AlLoRa.Security.identity import (load_or_create_identity, device_id_from_pubkey)
        from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
        path = self.config.get('identity_file', None)
        if path:
            self.identity_priv, self.identity_pub, self.device_id = \
                load_or_create_identity(path, urandom)
        else:
            self.identity_priv = generate_private_key(urandom)
            self.identity_pub = public_key_uncompressed(self.identity_priv)
            self.device_id = device_id_from_pubkey(self.identity_pub)
            if self.debug:
                print("no identity_file configured, using an ephemeral identity "
                      "(device_id changes each boot)")

    def _resolve_session_id(self):
        # The 1-byte session address. An explicit config value always wins (debugging, or the
        # Collector's on-clash reassignment). Otherwise it is identity-derived: device_id[0] in
        # secure, the device-specific low byte of the short MAC in open (never the OUI bytes).
        explicit = self.config.get('session_id', None)
        if explicit is not None:
            return explicit
        if self.security_mode in ('secure', 'strict'):
            self._ensure_identity()
            return self.device_id[0]
        return int(self.MAC[-2:], 16)

    # --- first-contact handshake over the wire (open device_id-addressed CTRL frames) -------
    # The exchange rides the shared request/respond verbs. Message kinds ride a 1-byte prefix
    # on the CTRL payload (provisional layout): the Collector (responder + sid-assigner) drives
    # two rounds, the Source (initiator) answers with its ephemeral key then completes. Frames
    # are addressed by the Source's device_id[:4] (no MAC on the wire); a MAC-registered peer
    # keeps the retiring two-MAC shape.
    _HS_INIT = 0        # Collector -> Source: begin (prompt for the ephemeral key)
    _HS_HELLO = 1       # Source -> Collector: ephemeral public key
    _HS_WELCOME = 2     # Collector -> Source: static public key + assigned sid
    _HS_ACK = 3         # Source -> Collector: session established

    def _ctrl_packet(self, token, hs_kind, payload=b"", addressing="did"):
        # A first-contact v3 CTRL frame (no sid until the handshake assigns one); the hybrid
        # codec puts it on the wire open. Addressed by device_id[:4] (v3, no MAC on the wire),
        # a single token stamped the same in both directions, or, for a MAC-registered peer,
        # by the retiring two-MAC shape.
        p = Packet_v3(mesh_mode=self.mesh_mode, addressing=addressing)
        if addressing == "did":
            p.set_did(token)
        else:
            p.set_source(self.MAC)
            p.set_destination(token)
        p.set_kind(Packet_v3.CTRL)
        p.set_payload(bytes([hs_kind]) + payload)
        return p


    def open_backup(self):
        with open(self.config_file, "r") as f:
            self.config = loads(f.read())

        self.name = self.config.get('name', "N")
        self.debug = self.config.get('debug', False)
        self.mesh_mode = self.config.get('mesh_mode', False)
        self.short_mac = self.config.get('short_mac', False)
        self.chunk_size = self.config.get('chunk_size', 235)

        # v3: protocol version + open-mode session addressing.
        # Defaults keep v2 behavior byte-for-byte (version 2, MAC addressing).
        self.protocol_version = self.config.get('protocol_version', 2)
        self.security_mode = self.config.get('security_mode', 'open')
        # session_id is resolved after init (identity-derived unless config overrides); see
        # _resolve_session_id. It is read from config here only as the explicit override source.
        self.addressing = 'sid' if self.protocol_version >= 3 else 'mac'

        self.config_connector_dic = self.config.get('connector', None)    #{"freq" : lora_config['freq'], "sf": lora_config['sf']}
        self.config_connector_dic['mesh_mode'] = self.mesh_mode
        self.config_connector_dic['short_mac'] = self.short_mac
        self.config_connector_dic['protocol_version'] = self.protocol_version
        self.config_connector_dic['addressing'] = self.addressing

        if self.debug:
            print(self.config)

    def backup_config(self):
        conf = {"name": self.name,
                "chunk_size": self.chunk_size,
                "mesh_mode": self.mesh_mode,
                "short_mac": self.short_mac,
                "debug": self.debug,
                "connector" : self.connector.backup_config()}
        with open(self.config_file, "w") as f:
            f.write(dumps(conf))

    def config_connector(self):
        self.connector.config(self.config_connector_dic)

        self.MAC = self.connector.get_mac()[-8:]
        self.status["MAC"] = self.MAC
        print(self.name, ":", self.MAC)

    def get_mesh_mode(self):
        return self.mesh_mode

    def new_packet(self):
        """Build an outgoing packet for the negotiated protocol version.

        v3 frames are session-id-addressed (the sid is pre-set here); v2 frames keep
        MAC addressing. Callers that need MAC fields (v2) set source/destination after.
        """
        if self.protocol_version >= 3:
            packet = Packet_v3(mesh_mode=self.mesh_mode, addressing=self.addressing)
            packet.set_session(self.session_id)
            return packet
        return Packet(self.mesh_mode, self.short_mac)

    def is_for_me(self, packet):
        if self.protocol_version >= 3:
            if packet.addressing == "did":   # v3 first contact: addressed to my device_id[:4]
                return self.device_id is not None and packet.get_did() == self.device_id[:4]
            if packet.addressing == "mac":   # legacy first-contact / handshake frame (no sid yet)
                return packet.get_destination() == self.MAC
            return packet.get_session() == self.session_id
        return packet.get_destination() == self.MAC

    # --- the shared one-round engine: request (initiator) / respond (responder) ------------
    # Both roles run the same round from opposite sides, so both verbs live here on the Node.
    # Role reversal is just a node calling the other verb (the drive-role swaps; the type and
    # the trust-anchor don't).

    def request(self, packet):
        """One initiator round: transmit a request and wait for its reply. Returns the
        connector's (response | error dict, size_sent, size_recv, td) tuple. Driven by
        Requester/Gateway to pull a transfer; a role-reversed Source runs it too."""
        return self.connector.send_and_wait_response(packet)

    def respond(self, handler):
        """One responder round: receive a request and, if it is addressed to me, hand it to
        `handler` (which produces and sends the reply); otherwise forward it (mesh relay).
        Returns the received request packet, or None if nothing arrived, so a role-specific
        driver keeps its own bookkeeping (sf-trial, timeout)."""
        packet = self.listen_requester()
        if packet is None:
            return None
        if self.is_for_me(packet):
            handler(packet)
        else:
            self.forward(packet)
        return packet

    # Receive one request and parse it, via the same connector.listen + codec.deframe seam the
    # initiator's send_and_wait_response uses (this is the de-dup: the responder no longer
    # hand-rolls recv + Packet.load).
    def listen_requester(self):
        focus_time = self.connector.adaptive_timeout
        data, td = self.connector.listen(focus_time)   # one timed window; td measured at the radio
        self.tr = time()   # when the request landed (send_response times the reply from here)

        if not data:
            if self.debug:
                print("No data received within focus time")

            self.connector.increase_adaptive_timeout()
            return None

        packet = self.connector.codec.deframe(data)
        if packet is None:
            # A frame we can't parse. deframe is safe (never throws), so like the initiator's
            # recv path we just treat it as no usable request and wait again.
            if self.debug:
                print("Could not parse frame: ", data)
            return None

        if self.mesh_mode:
            try:
                packet_id = packet.get_id()  # Check if already forwarded or sent by myself
                if packet_id in self.LAST_SEEN_IDS or packet_id in self.LAST_IDS:
                    if self.debug:
                        print("ALREADY_SEEN", self.LAST_SEEN_IDS)
                    return None
            except Exception as e:
                if self.debug:
                    print(e)

        if self.debug:
            rssi = self.connector.get_rssi()
            snr = self.connector.get_snr()
            print('LISTEN_REQUESTER({}) at: {} || request_content : {}'.format(td, self.connector.adaptive_timeout, packet.get_content()))
            print("RSSI: ", rssi, " SNR: ", snr)
            self.status['RSSI'] = rssi
            self.status['SNR'] = snr
            self.status['PSizeR'] = len(data)
            self.status['TimePR'] = td * 1000  # Time in ms

        self.connector.decrease_adaptive_timeout(td)

        return packet

    def send_response(self, response_packet: Packet):
        if response_packet:
            if self.mesh_mode:
                response_packet.set_id(self.generate_id())
            t0 = time()
            if self.connector.sf == 12:
                sleep(1)
            self.send_lora(response_packet)
            tf = time()
            time_send = tf - t0
            time_reply = tf - self.tr
            if self.debug:
                print("Time Send: ", time_send, " Time Reply: ", time_reply)
            if self.subscribers:
                self.status['PSizeS'] = len(response_packet.get_content())
                self.status['TimePS'] = time_send
                self.status['TimeBtw'] = time_reply
                self.notify_subscribers()

    def forward(self, packet: Packet):
        try:
            if packet.get_mesh():
                if self.debug:
                    print("FORWARDED", packet.get_content())

                random_sleep = 0
                if packet.get_sleep():
                    random_sleep = (urandom(1)[0] % 5 + 1) * 0.1

                if packet.get_debug_hops():
                    packet.add_hop(self.name, self.connector.get_rssi(), random_sleep)
                packet.enable_hop()
                if random_sleep:
                    sleep(random_sleep)

                success = self.send_lora(packet)
                if success:
                    self.LAST_SEEN_IDS.append(packet.get_id())
                    self.LAST_SEEN_IDS = self.LAST_SEEN_IDS[-self.MAX_IDS_CACHED:]
                else:
                    if self.debug:
                        print("ALREADY_FORWARDED", self.LAST_SEEN_IDS)
        except Exception as e:
            # If packet was corrupted along the way, won't read the COMMAND part
            if self.debug:
                print("ERROR FORWARDING", e)

    def generate_id(self):
        id = -1
        while (id in self.LAST_IDS) or (id == -1):
            id = int.from_bytes(urandom(2), 'little')
        self.LAST_IDS.append(id)
        self.LAST_IDS = self.LAST_IDS[-self.MAX_IDS_CACHED:]
        return id

    def check_id_list(self, id):
        if id in self.LAST_SEEN_IDS:
            return False
        self.LAST_SEEN_IDS.append(id)
        self.LAST_SEEN_IDS = self.LAST_SEEN_IDS[-self.MAX_IDS_CACHED:]
        return True

    def send_lora(self, packet):
        return self.connector.send(packet)

    def change_rf_config(self, new_config):
        print("Changing RF Config to: ", new_config)
        frequency = new_config.get("freq", None)
        sf = new_config.get("sf", None)
        bw = new_config.get("bw", None)
        cr = new_config.get("cr", None)
        tx_power = new_config.get("tx_power", None)
        chunk_size = new_config.get("cks", None)
        if self.debug:
            print("Changing RF Config to: ", frequency, sf, bw, cr, tx_power, chunk_size)
        changed = self.connector.change_rf_config(frequency=frequency, 
                                        sf=sf, bw=bw, cr=cr, 
                                        tx_power=tx_power)
        if chunk_size:
            self.chunk_size = chunk_size

        max_chunk_size = self.calculate_max_chunk_size()
        if self.chunk_size > max_chunk_size:
            self.chunk_size = max_chunk_size
            print("Chunk size too big, changing to: ", self.chunk_size)
            
        if changed:
            self.sf_trial = 15
            self.status["Freq"] = self.connector.frequency
            self.status["SF"] = self.connector.sf
            self.status["BW"] = self.connector.bw
            self.status["CR"] = self.connector.cr
            self.status["TX_P"] = self.connector.tx_power
            return True
        return False

    def calculate_max_chunk_size(self):
        if self.mesh_mode:
            if self.short_mac:
                header_size = Packet.HEADER_SIZE_MESH_SM
            else:
                header_size = Packet.HEADER_SIZE_MESH_LM
            #header_size = Packet.HEADER_SIZE_MESH
        else:
            if self.short_mac:
                header_size = Packet.HEADER_SIZE_P2P_SM
            else:
                header_size = Packet.HEADER_SIZE_P2P_LM
            #header_size = Packet.HEADER_SIZE_P2P
        return self.connector.get_max_payload_size() - header_size

    def restore_rf_config(self):
        self.connector.restore_rf_config()

    # Subscribers stuff:
    def register_subscriber(self, subscriber):
        if subscriber not in self.subscribers:
            self.subscribers.append(subscriber)

    def unregister_subscriber(self, subscriber):
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)

    def notify_subscribers(self):
        for subscriber in self.subscribers:
            subscriber.update(self.status)

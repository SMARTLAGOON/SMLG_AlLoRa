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
        self.session_id = self.config.get('session_id', 0)
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

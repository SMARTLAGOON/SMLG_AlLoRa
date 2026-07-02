import gc
from os import urandom
from AlLoRa.Nodes.Node import Node, Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Pacing import Pacing
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep, sleep_ms
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os

class Requester(Node):

    def __init__(self, connector = None, config_file = "LoRa.json", 
                    debug_hops = False, 
                    NEXT_ACTION_TIME_SLEEP = 0.1, 
                    max_sleep_time = 3, 
                    successful_interactions_required = 5):
        super().__init__(connector, config_file)
        gc.enable()
        
        self.debug_hops = debug_hops

        # The inter-request sleep controller lives in Pacing now (policy on the logic-holder,
        # mirroring the adaptive-window extraction). Pacing is fed the sf/bw-derived bounds;
        # the init cap is the caller's `max_sleep_time`, not the ToA-derived max — preserving
        # the original two-step init, where calculate_sleep_time_bounds' max was overwritten.
        min_sleep, _ = self.calculate_sleep_time_bounds()
        self.pacing = Pacing(successful_interactions_required=successful_interactions_required,
                             max_failures=3, exponential_backoff_threshold=0.5)
        self.pacing.set_sleep_bounds(min_sleep, max_sleep_time)

        if self.config:
            self.result_path = self.config.get('result_path', "Results")
            if self.debug:
                print("Result path: ", self.result_path)
            try:
                os.mkdir(self.result_path)
            except Exception as e:
                if self.debug:
                    print("Error creating result path: {}".format(e))

        self.status["SMAC"] = "-"   # Source MAC
        self.source_mac = None
        self.time_request = time()
        self._wire_chunk_size = None   # v3: sender's chunk_size, read from typed METADATA

    # `NEXT_ACTION_TIME_SLEEP` now lives in `Pacing.sleep`; this property keeps every call
    # site working (the loop's `finally`, `Gateway.check_digital_endpoints`, examples).
    @property
    def NEXT_ACTION_TIME_SLEEP(self):
        return self.pacing.sleep

    @NEXT_ACTION_TIME_SLEEP.setter
    def NEXT_ACTION_TIME_SLEEP(self, value):
        self.pacing.sleep = value

    def create_request(self, destination, mesh_active, sleep_mesh, session_id=None):
        if self.protocol_version >= 3:
            packet = self.new_packet()          # v3 frame, session-id addressed
            if session_id is not None:
                packet.set_session(session_id)
        else:
            packet = Packet(self.mesh_mode, self.short_mac)
            packet.set_source(self.connector.get_mac())
            packet.set_destination(destination)
        if mesh_active:
            packet.enable_mesh()
            if not sleep_mesh:
                packet.disable_sleep()
        return packet

    # def send_request(self, packet: Packet) -> Packet:
    #     if self.mesh_mode:
    #         packet.set_id(self.generate_id())
    #         if self.debug_hops:
    #             packet.enable_debug_hops()

    #     self.time_since_last_request = time() - self.time_request
    #     self.time_request = time()

    #     response_packet, packet_size_sent, packet_size_received, time_pr = self.connector.send_and_wait_response(packet)
        
    #     if self.subscribers:
    #         self.status['PSizeS'] = packet_size_sent
    #         self.status['PSizeR'] = packet_size_received
    #         self.status['TimePR'] = time_pr * 1000  # Time in ms
    #         self.status['TimeBtw'] = self.time_since_last_request * 1000  # Time in ms
    #         self.status['RSSI'] = self.connector.get_rssi()
    #         self.status['SNR'] = self.connector.get_snr()
    #         if response_packet is None:
    #             self.status['Retransmission'] += 1
    #             if packet_size_received > 0:
    #                 self.status['CorruptedPackets'] += 1

    #     return response_packet
    def send_request(self, packet: Packet) -> Packet:
        if self.mesh_mode and self.protocol_version < 3:   # v3 mesh uses seq, not a random id
            packet.set_id(self.generate_id())
            if self.debug_hops:
                packet.enable_debug_hops()

        self.time_since_last_request = time() - self.time_request
        self.time_request = time()

        # One initiator round via the shared verb (wraps the connector's send_and_wait_response).
        response_packet, packet_size_sent, packet_size_received, time_pr = self.request(packet)

        if self.subscribers:
            self.status['PSizeS'] = packet_size_sent
            self.status['PSizeR'] = packet_size_received
            self.status['TimePR'] = time_pr * 1000  # Time in ms
            self.status['TimeBtw'] = self.time_since_last_request * 1000  # Time in ms
            self.status['RSSI'] = self.connector.get_rssi()
            self.status['SNR'] = self.connector.get_snr()

            if isinstance(response_packet, dict):  # Handle errors
                self.status['Retransmission'] += 1
                if response_packet.get("type") == "CORRUPTED_PACKET":
                    self.status['CorruptedPackets'] += 1
                if self.debug:
                    print("Error received during request: ", response_packet)
                return None  # Signal failure

        elif isinstance(response_packet, dict):
            if self.debug:
                print("Error received during request: ", response_packet)
            return None

        return response_packet  # Return valid packet if successful

    def perform_handshake(self, digital_endpoint):
        """As the Collector (the handshake responder + sid-assigner), drive the two-round ECDH
        with a Source over open MAC CTRL frames and store the resulting Session. Returns the
        Session on success, or None if a round fails. The Source authenticates nothing here —
        in `secure` this is confidentiality-only; the gateway accepts the peer by its
        registered identity, and the catastrophic downlink is guarded separately."""
        from AlLoRa.Security.handshake import responder_accept
        from AlLoRa.Security.ec_p256 import generate_private_key
        if self.static_priv is None:
            self.static_priv = generate_private_key(urandom)

        peer = digital_endpoint.get_mac_address()
        sid = digital_endpoint.session_id

        # round 1: prompt the Source for its ephemeral public key
        hello = self.send_request(self._ctrl_packet(peer, Node._HS_INIT))
        if not self._is_hs(hello, Node._HS_HELLO):
            return None
        session, welcome = responder_accept(self.static_priv, hello.get_payload()[1:], sid)

        # round 2: send our static public key + the assigned sid, expect the ack
        ack = self.send_request(self._ctrl_packet(peer, Node._HS_WELCOME, welcome))
        if not self._is_hs(ack, Node._HS_ACK):
            return None

        self.session_store.put(session)
        return session

    @staticmethod
    def _is_hs(packet, hs_kind):
        # A valid handshake reply: a CTRL frame whose 1-byte type prefix matches.
        return (packet is not None and packet.get_command() == Packet_v3.CTRL
                and packet.get_payload() and packet.get_payload()[0] == hs_kind)

    def ask_ok(self, packet: Packet):
        packet.set_ok()
        response_packet = self.send_request(packet)
        if self.save_hops(response_packet):
            return  (1, "hop_catch.json"), response_packet.get_hop()
        if response_packet.get_command() == Packet.OK:
            hop = response_packet.get_hop()
            return True, hop
        return None, None

    def ask_metadata(self, packet: Packet):
        packet.ask_metadata()
        response_packet = self.send_request(packet)
        if self.save_hops(response_packet):
            return  (1, "hop_catch.json"), response_packet.get_hop()
        if response_packet.get_command() == Packet.METADATA:
            try:
                metadata = response_packet.get_metadata()
                self._wire_chunk_size = metadata.get("CHUNK_SIZE")  # v3 carries it; None for v2
                hop = response_packet.get_hop()
                length = metadata["LENGTH"]
                filename = metadata["FILENAME"]
                if self.subscribers:
                    self.status['File'] = filename
                return (length, filename), hop
            except:
                return None, None
        return None, None

    def ask_data(self, packet: Packet, next_chunk):
        packet.ask_data(next_chunk)
        response_packet = self.send_request(packet)
        if self.save_hops(response_packet):
            return b"0", response_packet.get_hop()
        if response_packet.get_command() == Packet.DATA:
            try:
                chunk = response_packet.get_payload()
                if self.mesh_mode:
                    id = response_packet.get_id()
                    if not self.check_id_list(id):
                        return None, None
                    hop = response_packet.get_hop()
                    if self.debug and hop:
                        print("CHUNK + HOP: {} -> {} - Node: {}".format(chunk, hop, self.source_mac))
                    return chunk, hop
                else: 
                    if self.debug:
                        print("CHUNK: {} - Node: {}".format(chunk, self.source_mac))
                    return chunk, None

            except Exception as e:
                if self.debug:
                    print("ASKING DATA ERROR: {} Node {}".format(e, self.source_mac))
                return None, None
        return None, None

    def listen_to_endpoint(self, digital_endpoint: Digital_Endpoint, listening_time=None,
                       print_file=False, save_file=False, one_file=False):
        stop = False

        mac = digital_endpoint.get_mac_address()
        self.source_mac = mac

        if self.subscribers:
            self.status['SMAC'] = mac
        save_to = self.result_path + "/" + mac
        sleep_mesh = digital_endpoint.get_sleep()

        connector_ok = self.prepare_connector(digital_endpoint)

        if not connector_ok:
            if self.debug:
                print("Connector not ready for endpoint: ", mac)
            return False

        t0 = time()
        if listening_time is None:
            listening_time = float('inf')
        end_time = t0 + (listening_time * 1000)
        
        while time() < end_time:
            t0 = time()
            
            try:
                packet_request = self.create_request(mac, digital_endpoint.get_mesh(), sleep_mesh,
                                                     digital_endpoint.session_id)

                if digital_endpoint.state == "REQUEST_DATA_STATE":
                    if self.debug:
                        print("ASKING METADATA to {}".format(mac))
                    metadata, hop = self.ask_metadata(packet_request)
                    t0 = time()
                    # v3 typed METADATA carries the sender's chunk_size; v2 falls back to
                    # our own configured chunk_size (the matched-config stopgap).
                    cks = self._wire_chunk_size or self.chunk_size
                    digital_endpoint.set_metadata(metadata, hop, self.mesh_mode, save_to, cks)
                    if self.debug:
                        print("METADATA from {}: {}".format(mac, metadata))

                elif digital_endpoint.state == "PROCESS_CHUNK_STATE":
                    next_chunk = digital_endpoint.get_next_chunk()
                    if next_chunk is not None:
                        if self.debug:
                            print("ASKING CHUNK: {} to {}".format(next_chunk, mac))
                        data, hop = self.ask_data(packet_request, next_chunk)
                        t0 = time()
                        self.status['Chunk'] = digital_endpoint.file_reception_info["total_chunks"] - next_chunk
                        file = digital_endpoint.set_data(data, hop, self.mesh_mode)
                        if file:
                            final_ok = self.create_request(mac, digital_endpoint.get_mesh(),
                                                           sleep_mesh, digital_endpoint.session_id)
                            final_ok.set_ok()
                            if self.protocol_version < 3:
                                final_ok.set_source(self.connector.get_mac())
                            sleep(1)
                            self.send_lora(final_ok)
                            self.status['Chunk'] = "DONE"
                            if print_file:
                                print(file.get_content())
                            if save_file:
                                file.save(save_to)
                            if one_file:
                                stop = True

                elif digital_endpoint.state == "OK":
                    if self.debug:
                        print("ASKING OK to {}".format(mac))
                    ok, hop = self.ask_ok(packet_request)
                    t0 = time()
                    digital_endpoint.connected(ok, hop, self.mesh_mode)

                if self.sf_trial:
                    if self.debug:
                        print("SF Trial ended successfully")
                    self.sf_trial = False
                    self.backup_config()

                self.pacing.on_success()

            except Exception as e:
                if self.debug:
                    print("LISTEN_TO_ENDPOINT ERROR: {} Node {}".format(e, mac))
                if self.sf_trial:
                    self.sf_trial -= 1
                    if self.sf_trial <= 0:
                        if self.debug:
                            print("Restoring RF config")
                        self.restore_rf_config()
                        self.sf_trial = False

                dt = (time() - t0) / 1000

                self.pacing.on_failure()

            finally:
                if self.subscribers:
                    self.status['Status'] = digital_endpoint.state
                    self.notify_subscribers()

                gc.collect()
                dt = (time() - t0) / 1000
                if self.debug:
                    print("DT: ", dt, "Sleep time: ", self.NEXT_ACTION_TIME_SLEEP)
                sleep_time = self.pacing.next_sleep()
                if self.debug:
                    print("Sleep time: ", sleep_time)
                if sleep_time > 0:
                    sleep(sleep_time)
                
                if stop:
                    break
            
    def save_hops(self, packet):
        if packet is None:
            return False
        if packet.get_debug_hops():
            hops = packet.get_message_path()
            id = packet.get_id()
            t = get_time()  #strftime("%Y-%m-%d_%H:%M:%S")
            line = "{}: ID={} -> {}\n".format(t, id, hops)
            with open('log_rssi.txt', 'a') as log:
                log.write(line)
            return True
        return False

    def ask_change_rf(self, digital_endpoint, new_sf):
        try_for = 3
        if 7 <= new_sf <= 12:
            while True:
                packet = Packet(self.mesh_mode, self.short_mac)
                packet.set_destination(digital_endpoint.get_mac_address())
                packet.set_change_rf(new_sf)
                if digital_endpoint.get_mesh():
                    packet.enable_mesh()
                    if not digital_endpoint.get_sleep():
                        packet.disable_sleep()
                response_packet = self.send_request(packet)
                if response_packet.get_command() == Packet.OK:
                    sf_response = int(response_packet.get_payload().decode().split('"')[1])
                    print(sf_response)
                    if sf_response == new_sf:
                        return True
                else:
                    try_for -= 1
                    if try_for <= 0:
                        return False

    def ask_change_rf(self, digital_endpoint, new_config):
        try_for = 20
        new_config = [new_config.get("freq", None), new_config.get("sf", None), 
                        new_config.get("bw", None), new_config.get("cr", None), 
                        new_config.get("tx_power", None), 
                        new_config.get("cks", None)] 
        config = self.connector.get_rf_config()
        if self.debug:
            print("Current config: ", config)
            print("New config: ", new_config)
        # Only change the values that are different from the current configuration
        new_freq = new_config[0] if new_config[0] != config[0] else None
        new_sf = new_config[1] if new_config[1] != config[1] else None
        new_bw = new_config[2] if new_config[2] != config[2] else None
        new_cr = new_config[3] if new_config[3] != config[3] else None
        new_tx_power = new_config[4] if new_config[4] != config[4] else None
        new_chunk_size = new_config[5] if new_config[5] != self.chunk_size else None
        while True:
            packet = Packet(self.mesh_mode, self.short_mac)
            packet.set_destination(digital_endpoint.get_mac_address())
            changes = packet.set_change_rf({"freq": new_freq, "sf": new_sf, 
                                            "bw": new_bw, "cr": new_cr, 
                                            "tx_power": new_tx_power,
                                            "cks": new_chunk_size})
            if not changes:
                return False
            if digital_endpoint.get_mesh():
                packet.enable_mesh()
                if not digital_endpoint.get_sleep():
                    packet.disable_sleep()
            response_packet = self.send_request(packet)
            try:
                if response_packet.get_command() == Packet.OK:
                    new_config = response_packet.get_config()
                    if self.debug:
                        print("OK and changing config to: ", new_config)
                    changed = self.change_rf_config(new_config)
                    if not changed:
                        return False
                        
                    self.notify_subscribers()
                    self.reset_sleep_time()
                    return True
                else:
                    try_for -= 1
                    if try_for <= 0:
                        return False
            except Exception as e:
                if self.debug:
                    print("Error changing RF config: ", e)
                try_for -= 1
                if try_for <= 0:
                    return False

    def reset_sleep_time(self):
        # Recompute the sf/bw-derived bounds (they may have changed with the RF config) and
        # hand them to Pacing, which resets the controller to a fresh hunt. Unlike init, the
        # max here is the ToA-derived one, matching the original reset_sleep_time.
        self.pacing.set_sleep_bounds(*self.calculate_sleep_time_bounds())
        if self.debug:
            print("Reset sleep time to:", self.NEXT_ACTION_TIME_SLEEP)


    def calculate_sleep_time_bounds(self):
        sf = self.connector.sf
        bw = self.connector.bw
        # Basic heuristic to calculate min and max sleep times based on SF and BW
        sf_factor = 2 ** (sf - 7)  # SF7 as baseline
        bw_factor = 250 / bw  # 500kHz as baseline
        base_min_sleep_time = 0.001  # Adjust as needed
        base_max_sleep_time = 0.5  # Adjust as needed
        min_sleep_time = base_min_sleep_time * bw_factor / sf_factor
        max_sleep_time = base_max_sleep_time * sf_factor / bw_factor
        if self.debug:
            print("Min sleep time: ", min_sleep_time, "Max sleep time: ", max_sleep_time)
        return min_sleep_time, max_sleep_time

    def prepare_connector(self, digital_endpoint):
        if self.debug:
            print("Preparing connector for endpoint: ", digital_endpoint)
        de_freq = digital_endpoint.freq
        de_sf = digital_endpoint.sf
        de_bw = digital_endpoint.bw
        de_cr = digital_endpoint.cr
        de_tx_power = digital_endpoint.tx_power

        freq, sf, bw, cr, tx_power = self.connector.get_rf_config()
        print("Current RF config: ", freq, sf, bw, cr, tx_power)
        print("Endpoint RF config: ", de_freq, de_sf, de_bw, de_cr, de_tx_power)
        if de_freq != freq or de_sf != sf or de_bw != bw or de_cr != cr or de_tx_power != tx_power:
            if self.debug:
                print("Changing RF config to: ", de_freq, de_sf, de_bw, de_cr, de_tx_power)
            # try 3 times to change the RF config to fit the endpoint configuration
            for i in range(3):
                success = self.connector.change_rf_config(frequency=de_freq, sf=de_sf, bw=de_bw, cr=de_cr, tx_power=de_tx_power)
                if success:
                    sleep(1)
                    for i in range(3):
                        rf_params = self.connector.get_rf_config()
                        if rf_params:
                            # Check that the RF configuration has been changed successfully
                            if rf_params[0] == de_freq and rf_params[1] == de_sf and rf_params[2] == de_bw and rf_params[3] == de_cr and rf_params[4] == de_tx_power:
                                if self.debug:
                                    print("RF configuration changed successfully")
                                return True
                            break
                        sleep(1)
                sleep(1)
            if self.debug:
                print("Failed to change RF configuration")
            return False    # Failed to change RF configuration
        if self.debug:
            print("RF config already set to endpoint config")
        return True
            




from AlLoRa.Packet import Packet
from AlLoRa.Pacing import Pacing
from AlLoRa.Codec import build_codec
import gc
from math import ceil
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep, sleep_ms
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os
from os import urandom

class Connector:
    MAX_LENGTH_MESSAGE = 255

    def __init__(self):
        self.MAC = "00000000"
        self.pacing = Pacing()   # one home for the adaptive receive window (was adaptive_timeout)
        self.codec = build_codec()   # how a Packet is spoken on the wire; rebuilt in config()
        self.debug = False
        self.protocol_version = 2    # config() decides; this is its default until it runs
        self.frame_size = None       # set by the node once it knows its clamped chunk size
        # A frame the radio received and threw away as damaged is not the same event as a
        # window in which nothing arrived, and the two deserve different diagnoses. A
        # connector whose radio can tell the difference raises this during recv; listen()
        # lowers it before every window, so a connector that never sets it (all but the
        # SX127x one today) keeps reporting plain timeouts exactly as it always did.
        self.recv_dropped_corrupt = False

    # `adaptive_timeout` / `observed_min_timeout` now live in `Pacing`; these properties keep
    # every existing call site (the send loop, the Edge's pokes, the tunnels) working.
    @property
    def adaptive_timeout(self):
        return self.pacing.window

    @adaptive_timeout.setter
    def adaptive_timeout(self, value):
        self.pacing.window = value

    @property
    def observed_min_timeout(self):
        return self.pacing.observed_min_timeout

    @observed_min_timeout.setter
    def observed_min_timeout(self, value):
        self.pacing.observed_min_timeout = value

    def config(self, config_json):
        # JSON Example:
        # {
        #     "name": "N",
        #     "frequency": 868,
        #     "sf": 7,
        #     "mesh_mode": false,
        #     "debug": false,
        #     "min_timeout": 0.5,
        #     "max_timeout": 6
        # }
        self.config_parameters = config_json
        if self.config_parameters:
            self.name = self.config_parameters.get('name', "N")
            self.debug = self.config_parameters.get('debug', False)

            self.frequency = self.config_parameters.get('freq', 868)    # 868 MHz
            self.sf = self.config_parameters.get('sf', 7)               # SF7
            self.bw = self.config_parameters.get("bandwidth", 125)            # 125 kHz
            self.cr = self.config_parameters.get("coding_rate", 1)            # 4/5
            self.tx_power = self.config_parameters.get("tx_power", 14)         # 14 dBm

            self.mesh_mode = self.config_parameters.get('mesh_mode', False)
            self.short_mac = self.config_parameters.get('short_mac', False)

            # v3: which codec parses replies, and how a reply is matched to its request.
            self.protocol_version = self.config_parameters.get('protocol_version', 2)
            self.addressing = self.config_parameters.get('addressing', 'mac')

            self.min_timeout = self.config_parameters.get('min_timeout', 0.5)
            self.max_timeout = self.config_parameters.get('max_timeout', 6)
            self.timeout_delta = self.config_parameters.get('timeout_delta', 1)  # Delta for processing times
        
            # Calculate initial adaptive timeouts
            self.update_timeouts()

            self.adaptive_timeout = self.max_timeout
            self.backup_timeout = self.adaptive_timeout
            self.backup_rf_config()

            # Build the codec for the negotiated version x posture. Secure posture wiring
            # (session resolver + AEAD backend) threads through here when secure goes live;
            # today every configured node is open, so this picks v2 or v3-open.
            self.codec = build_codec(
                protocol_version=self.protocol_version,
                addressing=self.addressing,
                mesh_mode=self.mesh_mode,
                short_mac=self.short_mac,
                my_mac=self.get_mac(),
            )
        else:
            if self.debug:
                print("Error: No config parameters")

    def set_secure(self, session_resolver, aead):
        """Switch the codec to secure mode: it seals/opens each frame with the Session the
        resolver returns for the frame's sid (`sid -> Session | None`), using the given AEAD
        backend. Called by a secure-posture Node once it has a session store + a live backend;
        the wire config (version/addressing/mesh) is unchanged, only the posture."""
        self.codec = build_codec(
            protocol_version=self.protocol_version, addressing=self.addressing,
            mesh_mode=self.mesh_mode, short_mac=self.short_mac, my_mac=self.get_mac(),
            security_mode='secure', session_resolver=session_resolver, aead=aead)

    def get_max_payload_size(self):
        """The largest frame the radio will carry, in bytes.

        The LoRa explicit-header PHY carries 255 bytes at every spreading factor and every
        bandwidth, and that is what v3 reports. The table this replaced returned 111 at SF11
        and 30 at SF12; those numbers were measured on hardware that never had
        LowDataRateOptimize set, where long frames at high spreading factors genuinely did
        fail, so they described that fault rather than the PHY. With the register written a
        raw sweep put 18 of 18 frames intact at 255 bytes at SF12. The table was also blind
        to bandwidth, which nothing noticed because the configured bandwidth was never
        reaching the radio either.

        v2 keeps the table it shipped with. It is frozen, and it is the baseline the v3
        throughput comparison is measured against, so moving it would rewrite the instrument
        rather than the protocol.
        """
        if self.protocol_version >= 3:
            return self.MAX_LENGTH_MESSAGE
        if self.sf < 11:
            return 255
        elif self.sf == 11:
            return 111
        elif self.sf == 12:
            return 30   #51

    def set_frame_size(self, frame_size):
        """Tell the connector how big the frames this node really sends are, so the receive
        window is sized for its traffic instead of for the ceiling.

        The node owns this number: the clamp that produces it needs the codec's per-frame
        overhead, which the node asks for. Recomputing the timeouts here keeps the window
        honest whenever the chunk size moves, which RF-config coordination does at runtime.
        """
        self.frame_size = frame_size
        self.update_timeouts()

    def update_timeouts(self):
        # Calculate the min and max timeouts based on the ToA for the current RF settings
        self.max_payload_size = self.get_max_payload_size()
        # The floor is what this node's own traffic costs; the ceiling is what the PHY can
        # still put in front of it. Those used to be one number because the ceiling was
        # small enough that nothing could exceed it, and they must not be collapsed again:
        # the adaptive window can never grow past the ceiling, so a node whose peer sends
        # larger frames than it does would be unable to receive them at all. Sizing only the
        # floor from real traffic is what stops a 30-byte-chunk node at SF12 from sitting on
        # a 7.7-second floor waiting out frames it never sends.
        floor_payload = self.max_payload_size
        if self.frame_size:
            floor_payload = min(floor_payload, self.frame_size)
        min_toa = self.calculate_toa(self.sf, self.bw, self.cr, floor_payload)
        max_toa = self.calculate_toa(self.sf, self.bw, self.cr, self.max_payload_size) * 2
        self.min_timeout = min_toa + self.timeout_delta # Convert ms to seconds
        self.max_timeout = max_toa + self.timeout_delta  # Convert ms to seconds and add delta for processing times
        self.pacing.set_bounds(self.min_timeout, self.max_timeout)   # bounds change -> window resets to max (as before)
        if self.debug:
            print("Updated timeouts: Min: {} s, Max: {} s".format(self.min_timeout, self.max_timeout))

    def calculate_toa(self, sf, bw, cr, payload_size):
        if self.debug:
            print("TOA with SF:", sf, "BW:", bw, "CR:", cr, "Payload:", payload_size)
        crc = 1  # CRC enabled
        bw_hz = bw * 1000   # Convert bandwidth to Hz
        t_symbol = (2 ** sf) / bw_hz    # Symbol duration
        t_preamble = t_symbol * (8 + 4.25)  # Preamble duration
        h = 0   # Implicit header disabled
        # Low data rate optimization. The radio turns this on once a symbol lasts longer
        # than 16 ms, which depends on bandwidth as well as spreading factor: it is SF11
        # and SF12 at BW125, but only SF12 at BW250, and it starts as low as SF10 at
        # BW62.5. Keying off the spreading factor alone made the airtime estimate wrong
        # wherever the bandwidth was not 125 kHz.
        de = 1 if t_symbol > 0.016 else 0
        cr_rate = cr / 4.0  # Coding rate
        # Payload Symbol Calculation
        payload_bits = 8 * payload_size - 4 * sf + 28 + 16 * (1 if crc else 0) - 20 * h
        bits_per_symbol = 4 * (sf - 2 * de)
        n_payload = 8 + max(0, int(ceil(payload_bits / bits_per_symbol) * (cr_rate + 4)))
        
        # Payload Duration
        t_payload = t_symbol * n_payload
        
        # Total Time on Air
        t_air = t_preamble + t_payload
        if self.debug:
            print("TOA:", t_air)
        
        return t_air

    def backup_config(self):
        # Report the LIVE RF config, not the values first loaded from disk. change_rf_config
        # moves the radio (self.frequency/sf/bw/cr/tx_power) without touching config_parameters,
        # so persisting the raw config_parameters would drop a committed trial: the node would
        # reboot on the old radio config. Overlay the live values under their canonical
        # LoRa.json keys, preserving every other connector key (timeouts, transport sub-config).
        conf = dict(self.config_parameters) if self.config_parameters else {}
        conf["freq"] = self.frequency
        conf["sf"] = self.sf
        conf["bandwidth"] = self.bw
        conf["coding_rate"] = self.cr
        conf["tx_power"] = self.tx_power
        return conf

    def get_mac(self):
        return self.MAC

    def set_mesh_mode(self, mesh_mode=False):
        self.mesh_mode = mesh_mode

    def transmit(self, wire):
        # Pure byte send: the narrowed transport primitive. Concrete connectors override
        # this (a radio put-on-air, a loopback queue); the base is a stub like recv.
        return None

    def recv(self, focus_time=12):
        return None

    def send(self, packet: Packet):
        # Back-compat: frame via the codec, then transmit the bytes. Connectors now override
        # transmit(wire); this keeps every send(packet) caller (Node.send_lora and up) working
        # and routes them through the same framing home.
        return self.transmit(self.codec.frame(packet))

    def listen(self, window):
        # One timed receive window, measured at the radio: the (wire, td) a split-Connector
        # tunnel bridge would run remotely and report back up (td is the round-trip the
        # policy layer feeds to Pacing).
        self.recv_dropped_corrupt = False    # this window's verdict, not the last one's
        t0 = time()
        wire = self.recv(window)
        td = (time() - t0) / 1000
        return wire, td

    def exchange(self, wire, window, match_key):
        """Transmit a frame and wait, at the radio, for the reply that satisfies `match_key`,
        within a shrinking window. Codec-free and keyless: it matches on the wire prefix
        (`match_key.matches_wire`), so a dumb tunnel bridge can run it and never ferry a
        foreign frame across the link. Returns `(reply_wire, td, status)` with status in
        matched / timeout / exhausted / send_error.

        This is the transport verb a split Connector's bridge half (an Adapter) serves (D↓/td↑). The
        local engine keeps composing transmit/listen with the codec directly, so its
        corrupt-vs-foreign error taxonomy (which needs the codec) is unchanged.
        """
        if not self.transmit(wire):
            return None, 0, "send_error"
        focus = window
        td = 0
        while focus > 0:
            wire_in, td = self.listen(focus)
            if not wire_in:
                return None, td, "timeout"
            if match_key.matches_wire(wire_in):
                return wire_in, td, "matched"
            # A frame whose addressing bytes aren't ours (foreign, or corruption that hit the
            # addressing prefix): keep waiting within the window rather than ferrying it up.
            focus = window - td
            if focus < self.min_timeout:
                return None, td, "exhausted"
        return None, td, "exhausted"

    def increase_adaptive_timeout(self):
        self.pacing.on_timeout()

    def decrease_adaptive_timeout(self, td):
        self.pacing.on_reply(td)
    
    def send_and_wait_response(self, packet):
        focus_time = self.adaptive_timeout
        wire = self.codec.frame(packet)   # framing goes live on the send path here (was in self.send)
        packet_size_sent = len(wire)
        # How a reply is matched to this request (version/posture-agnostic; the sid or MAC
        # mirror the request). Built once: the request doesn't change across the wait loop.
        match = self.codec.match_spec(packet)
        try:
            send_success = self.transmit(wire)
            if not send_success:
                error_info = {
                    "type": "SEND_ERROR",
                    "message": "Error sending packet",
                    "focus_time": focus_time,
                    "adaptive_timeout": self.adaptive_timeout,
                }
                if self.debug:
                    print(error_info["message"])
                return error_info, packet_size_sent, 0, 0
        except Exception as e:
            error_info = {
                "type": "EXCEPTION",
                "message": "Exception during send: {}".format(e),
                "focus_time": focus_time,
                "adaptive_timeout": self.adaptive_timeout,
            }
            if self.debug:
                print(error_info["message"])
            self.increase_adaptive_timeout()
            return error_info, packet_size_sent, 0, 0

        while focus_time > 0:
            try:
                received_data, td = self.listen(focus_time)   # one timed window at the radio
            except Exception as e:
                error_info = {
                    "type": "EXCEPTION",
                    "message": "Exception during recv: {}".format(e),
                    "focus_time": focus_time,
                    "adaptive_timeout": self.adaptive_timeout,
                }
                if self.debug:
                    print(error_info["message"])
                return error_info, packet_size_sent, 0, 0

            packet_size_received = len(received_data) if received_data else 0

            if not received_data:
                if self.recv_dropped_corrupt:
                    # The reply did arrive, damaged, and the radio dropped it. Report it as
                    # the corrupt frame it was: the node counts CorruptedPackets from this
                    # label, and that count is how a link's corruption is compared between
                    # runs. Reporting silence instead would hide it in the retransmissions.
                    # The window is left alone deliberately: the frame arrived inside it, so
                    # it was wide enough, and growing it would slow every later round for a
                    # reason that is not true.
                    error_info = {
                        "type": "CORRUPTED_PACKET",
                        "message": "Reply dropped at the radio: the payload CRC failed",
                        "focus_time": focus_time,
                        "time_difference": td,
                        "adaptive_timeout": self.adaptive_timeout,
                    }
                    if self.debug:
                        print(error_info["message"])
                    return error_info, packet_size_sent, packet_size_received, td
                error_info = {
                    "type": "TIMEOUT",
                    "message": "No response received",
                    "focus_time": focus_time,
                    "time_difference": td,
                    "adaptive_timeout": self.adaptive_timeout,
                }
                if self.debug:
                    print(error_info["message"])
                self.increase_adaptive_timeout()
                return error_info, packet_size_sent, packet_size_received, td

            if self.debug:
                print("WAIT_RESPONSE({}) at: {}|| source_reply: {}".format(td, self.adaptive_timeout, received_data))
            try:
                response_packet = self.codec.deframe(received_data)
                if response_packet is not None:
                    if match.matches(response_packet):
                        if len(received_data) > response_packet.HEADER_SIZE + 60:  # Hardcoded for only chunks
                            self.decrease_adaptive_timeout(td)
                        if response_packet.get_debug_hops():
                            response_packet.add_hop(self.name, self.get_rssi(), 0)
                        return response_packet, packet_size_sent, packet_size_received, td
                    # A well-formed frame that isn't our reply (a foreign/stale packet):
                    # keep waiting within the window rather than giving up.
                else:
                    if len(received_data) > 0:
                        error_info = {
                            "type": "CORRUPTED_PACKET",
                            "message": "{}".format(received_data),
                        }
                    else:
                        error_info = {
                            "type": "NO_PACKET",
                            "message": "No packet received",
                        }
                    if self.debug:
                        print(error_info["message"])
                    return error_info, packet_size_sent, packet_size_received, td

            except Exception as e:
                error_info = {
                    "type": "EXCEPTION",
                    "message": "Exception during packet load: {}, data: {}".format(e, received_data),
                }
                if self.debug:
                    print(error_info["message"])
                return error_info, packet_size_sent, packet_size_received, td

            focus_time = self.adaptive_timeout - td
            if focus_time < self.min_timeout:
                focus_time = self.min_timeout
                error_info = {
                    "type": "MIN_TIMEOUT_REACHED",
                    "message": "Minimum timeout reached, can't wait more",
                    "focus_time": focus_time,
                }
                if self.debug:
                    print(error_info["message"])
                return error_info, packet_size_sent, packet_size_received, td

    # This function returns the RSSI of the last received packet
    def get_rssi(self):
        return 0

    # This function returns the SNR of the last received packet
    def get_snr(self):
        return 0

    def signal_estimation(self):
        percentage = 0
        rssi = self.get_rssi()
        if (rssi >= -50):
            percentage = 100
        elif (rssi <= -50) and (rssi >= -100):
            percentage = 2 * (rssi + 100)
        elif (rssi < 100):
            percentage = 0
        if self.debug:
            print('SIGNAL STRENGTH', percentage, '%')
        return percentage

    def get_rf_config(self):
        return [self.frequency, self.sf, self.bw, self.cr, self.tx_power]

    def change_rf_config(self, frequency=None, sf=None, bw=None, cr=None, tx_power=None, backup=True):
        if backup:
            self.backup_rf_config()
        try:
            if frequency is not None:
                self.set_frequency(frequency)
            if sf is not None:
                self.set_sf(sf)
            if bw is not None:
                self.set_bw(bw)
            if cr is not None:
                self.set_cr(cr)
            if tx_power is not None:
                self.set_transmission_power(tx_power)
            self.update_timeouts()
            self.adaptive_timeout = self.max_timeout
            return True
        except Exception as e:
            if self.debug:
                print("Error changing RF config: ", e)
            self.restore_rf_config()
            return False

    def backup_rf_config(self):
        self.last_rf_config = [self.frequency, 
                                    self.sf, 
                                    self.bw, 
                                    self.cr, 
                                    self.tx_power]

    def update_rf_params(self, params):
        """
        Updates the connector's RF parameters.
        Override in derived classes if needed.
        """
        self.frequency = params.get("frequency", self.frequency)
        self.sf = params.get("sf", self.sf)
        self.bw = params.get("bw", self.bw)
        self.cr = params.get("cr", self.cr)
        self.tx_power = params.get("tx_power", self.tx_power)
        if self.debug:
            print("Updated RF parameters:", self.get_rf_config())

    def restore_rf_config(self):
        frequency =  self.last_rf_config[0]
        sf = self.last_rf_config[1]
        bw = self.last_rf_config[2] 
        cr = self.last_rf_config[3]
        tx_power = self.last_rf_config[4]
        self.change_rf_config(frequency=frequency, 
                                sf=sf, 
                                bw=bw, 
                                cr=cr, 
                                tx_power=tx_power,
                                backup=False)


    def set_frequency(self, frequency):
        pass

    def set_sf(self, sf):
        pass

    def set_bw(self, bw):
        pass

    def set_cr(self, cr):
        pass

    def set_transmission_power(self, tx_power):
        pass
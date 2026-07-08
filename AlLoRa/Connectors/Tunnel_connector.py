"""Tunnel_connector — the logic-holder half of a split Connector.

A tunnel runs the protocol engine, codec and keys on one device (a Raspberry-Pi gateway) and
the radio on another (an ESP32 Adapter) across a serial/WiFi link. This is the half on the
logic-holder: a real Connector whose transport verbs are forwarded over a Link to the bridge,
which runs them on its radio and returns the result. Because only opaque wire crosses the
link — never a parsed Packet — v2 / v3-open / v3-secure all tunnel unchanged, closing the
old "tunnels only speak v2" gap. The codec and session keys stay entirely here; the bridge is
dumb and keyless.

Two things move against the old design's fused override:
  * the wait-for-the-reply loop runs *at the radio* via `exchange` — only the matching reply
    (plus the radio-measured `td`) crosses the slow link, never a foreign frame, never a
    ping-ponged receive window;
  * the window `D` is sent *down* and the true `td` comes *up*, so pacing is owned here from a
    real measurement — retiring the old `ACK:<timeout>+0.5` guess where the far side reported
    its own timeout.
"""
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.Links import tunnel_rpc
from AlLoRa.utils.debug_utils import print


class Tunnel_connector(Connector):

    def __init__(self, link=None, rpc_timeout=20, link_margin=5):
        super().__init__()
        self.link = link
        # A control verb (rf/mac) round-trips fast; a listen/exchange verb blocks at the bridge
        # for up to the window, so its link deadline is window + slack for link + processing.
        self._rpc_timeout = rpc_timeout
        self._link_margin = link_margin

    # --- transport verbs, forwarded to the bridge radio over the link ---

    def transmit(self, wire):
        reply = self.link.rpc(tunnel_rpc.encode_transmit(wire), timeout=self._rpc_timeout)
        return tunnel_rpc.decode_bool_reply(reply) if reply else False

    def listen(self, window):
        reply = self.link.rpc(tunnel_rpc.encode_listen(window),
                              timeout=window + self._link_margin)
        if not reply:
            return None, window          # link gave up: treat as a radio timeout of ~one window
        return tunnel_rpc.decode_listen_reply(reply)

    def recv(self, focus_time=12):
        return self.listen(focus_time)[0]

    def exchange(self, wire, window, match_key):
        # The bridge runs the whole match loop at its radio; we send only the keyless prefix
        # down and get the matching reply (or a timeout) + the radio-measured td back up.
        prefix = match_key.wire_prefix()
        reply = self.link.rpc(tunnel_rpc.encode_exchange(wire, window, prefix),
                              timeout=window + self._link_margin)
        if not reply:
            return None, window, "timeout"
        return tunnel_rpc.decode_exchange_reply(reply)

    def send_and_wait_response(self, packet):
        """The initiator round over the tunnel: frame here, match at the radio via `exchange`,
        deframe the returned reply here. Same (response | error-dict, size_sent, size_recv, td)
        contract the engine reads for a local radio, so Requester/Source are untouched."""
        focus_time = self.adaptive_timeout
        wire = self.codec.frame(packet)
        packet_size_sent = len(wire)
        match = self.codec.match_spec(packet)

        try:
            reply_wire, td, status = self.exchange(wire, focus_time, match)
        except Exception as e:
            if self.debug:
                print("Tunnel exchange raised: {}".format(e))
            self.increase_adaptive_timeout()
            return ({"type": "EXCEPTION", "message": "Tunnel exchange: {}".format(e)},
                    packet_size_sent, 0, 0)

        if status == "send_error":
            return ({"type": "SEND_ERROR", "message": "Bridge failed to transmit"},
                    packet_size_sent, 0, 0)

        if not reply_wire:                       # timeout / exhausted at the radio
            self.increase_adaptive_timeout()
            return ({"type": "TIMEOUT", "message": "No response received", "time_difference": td},
                    packet_size_sent, 0, td)

        packet_size_received = len(reply_wire)
        # The bridge matched on the cleartext prefix only; the codec (here, with the keys) has
        # the final say — a right-prefix-but-corrupt or unverifiable frame deframes to None.
        response_packet = self.codec.deframe(reply_wire)
        if response_packet is None:
            return ({"type": "CORRUPTED_PACKET", "message": "{}".format(reply_wire)},
                    packet_size_sent, packet_size_received, td)

        if packet_size_received > response_packet.HEADER_SIZE + 60:   # a chunk-sized reply
            self.decrease_adaptive_timeout(td)
        if response_packet.get_debug_hops():
            response_packet.add_hop(self.name, self.get_rssi(), 0)
        return response_packet, packet_size_sent, packet_size_received, td

    # --- RF config + identity, forwarded to the bridge radio ---

    def change_rf_config(self, frequency=None, sf=None, bw=None, cr=None, tx_power=None,
                         backup=True):
        reply = self.link.rpc(
            tunnel_rpc.encode_set_rf(freq=frequency, sf=sf, bw=bw, cr=cr, tx=tx_power),
            timeout=self._rpc_timeout)
        if not reply or not tunnel_rpc.decode_bool_reply(reply):
            return False
        # Keep the logic-holder's RF view in sync so Pacing sizes the window `D` from the live
        # sf/bw (in a tunnel there is no local chip to read them back from).
        if frequency is not None:
            self.frequency = frequency
        if sf is not None:
            self.sf = sf
        if bw is not None:
            self.bw = bw
        if cr is not None:
            self.cr = cr
        if tx_power is not None:
            self.tx_power = tx_power
        self.update_timeouts()
        self.adaptive_timeout = self.max_timeout
        return True

    def get_rf_config(self):
        reply = self.link.rpc(tunnel_rpc.encode_get_rf(), timeout=self._rpc_timeout)
        if not reply:
            return []
        return tunnel_rpc.decode_get_rf_reply(reply)

    def request_mac(self):
        """Fetch the bridge radio's MAC (the real on-air address the peer answers to) and cache
        it as this connector's identity, so the codec's addressing/matching uses it."""
        reply = self.link.rpc(tunnel_rpc.encode_get_mac(), timeout=self._rpc_timeout)
        if reply:
            mac = tunnel_rpc.decode_get_mac_reply(reply)
            if mac:
                self.MAC = mac
        return self.MAC

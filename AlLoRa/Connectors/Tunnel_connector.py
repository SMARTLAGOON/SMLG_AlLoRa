"""Tunnel_connector: the logic-holder half of a split Connector.

A tunnel runs the protocol engine, codec and keys on one device (a Raspberry-Pi gateway) and
the radio on another (an ESP32 Adapter) across a serial/WiFi link. This is the half on the
logic-holder: a real Connector whose transport verbs are forwarded over a Link to the bridge,
which runs them on its radio and returns the result. Because only opaque wire crosses the
link (never a parsed Packet), v2 / v3-open / v3-secure all tunnel unchanged, closing the
old "tunnels only speak v2" gap. The codec and session keys stay entirely here; the bridge is
dumb and keyless.

Two things move against the old design's fused override:
  * the wait-for-the-reply loop runs *at the radio* via `exchange`: only the matching reply
    (plus the radio-measured `td`) crosses the slow link, never a foreign frame, never a
    ping-ponged receive window;
  * the window `D` is sent *down* and the true `td` comes *up*, so pacing is owned here from a
    real measurement, retiring the old `ACK:<timeout>+0.5` guess where the far side reported
    its own timeout.
"""
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.Codec import build_codec
from AlLoRa import tunnel_codec
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.time_utils import current_time_ms as _now_ms


class Tunnel_connector(Connector):

    # How many link deadlines in a row mean the bridge has stopped talking, rather than one
    # frame having been missed. One is far too eager: the far side is a small board serving a
    # radio, and a single slow reply would otherwise reboot it in the middle of a healthy
    # transfer. Nothing recovers until the link has been silent this many verbs running.
    LINK_FAILURES_BEFORE_RECOVERY = 3

    # Where the call id wraps. Two bytes keeps the number short in a JSON frame that crosses a
    # 9600-baud wire, and the only thing an id has to do is differ from the calls close enough
    # in time to still be in flight. Those are a handful, never thousands.
    REQ_ID_WRAP = 65536

    def __init__(self, link=None, rpc_timeout=20, link_margin=5):
        super().__init__()
        self.link = link
        # A control verb (rf/mac) round-trips fast; a listen/exchange verb blocks at the bridge
        # for up to the window, so its link deadline is window + slack for link + processing.
        self._rpc_timeout = rpc_timeout
        self._link_margin = link_margin
        self._link_failures = 0
        # The outcome of the last recovery attempt: True, False, or None for "never tried".
        # A caller that wants to log or give up on a dead adapter reads it here; the node's
        # loop is deliberately not interrupted by a recovery either way.
        self.link_recovered = None
        self._req_id = 0
        # What the bridge measured on the last frame it actually received, carried home in the
        # reply. None until a frame has been heard, and None again after a window that heard
        # nothing: a reading belongs to a frame, and there is no reading for silence.
        self._last_rssi = None
        self._last_snr = None

    def _next_req_id(self):
        self._req_id = (self._req_id + 1) % self.REQ_ID_WRAP
        return self._req_id

    @staticmethod
    def _answers(reply, req_id):
        """Whether `reply` is the answer to the call numbered `req_id`.

        An un-numbered reply is accepted rather than discarded. That is what a bridge flashed
        before ids existed sends, and refusing it would turn an upgrade of this half alone into
        a link that never works. It costs nothing that was not already being paid: such a reply
        is exactly as checkable as every reply was before.
        """
        if req_id is None:
            return True
        got = tunnel_codec.reply_id(reply)
        return got is None or got == req_id

    def config(self, config_json):
        super().config(config_json)
        # Reply matching in MAC addressing (v2, and the retiring v3-secure MAC-compat handshake)
        # keys off *my* on-air address, which for a tunnel is the bridge radio's MAC, not this
        # host's. The codec was just built with the placeholder MAC, so fetch the real one from
        # the bridge and rebuild. sid / device_id addressing (the v3 default) is MAC-independent,
        # so the tunnel skips this extra round trip there.
        if getattr(self, "addressing", "mac") == "mac" and self.link is not None:
            self.request_mac()

    # --- the link, and noticing when it has stopped answering ---

    def _rpc(self, request, timeout, req_id=None):
        """Every verb goes through here, so one place counts how long the bridge has been quiet.

        A `None` back from the link is not a radio timeout: an empty receive window comes back
        as a real reply frame carrying no wire. `None` means the *bridge* did not answer at all,
        which on a healthy link should never happen. Enough of those in a row and the adapter is
        wedged, or has been unplugged, or rebooted into something that is not listening.

        A raise counts the same as a silence, and on the wiring this exists for it is the more
        common of the two. A board reached over USB takes the serial device with it when it
        goes, so the very next read is not a timeout but an I/O error on a descriptor that no
        longer refers to anything. Reading that as a fatal error rather than as a failed verb
        would take down a node holding sessions the adapter knows nothing about.

        It is also where a reply that answers an earlier call is thrown away. Such a frame is
        well-formed and arrives exactly where the real answer would, so nothing downstream can
        tell it apart once it is returned; only its id can, and only here, where the id that was
        sent is still known. Discarding one costs the rest of this verb's own deadline and no
        more, so a bridge finishing an old window delays a call rather than corrupting it. A
        deadline that runs out mid-discard is reported as a silent bridge, which it is: nothing
        answering *this* call arrived in time.
        """
        deadline = _now_ms() + int(timeout * 1000)
        try:
            reply = self.link.rpc(request, timeout=timeout)
            while reply is not None and not self._answers(reply, req_id):
                if self.debug:
                    print("Discarding a reply to an earlier call (wanted {})".format(req_id))
                remaining = deadline - _now_ms()
                if remaining <= 0:
                    reply = None
                    break
                reply = self.link.read_reply(timeout=remaining / 1000.0)
        except Exception as e:
            if self.debug:
                print("Link rpc raised: {}".format(e))
            reply = None
        if reply:
            self._link_failures = 0
            return reply
        self._link_failures += 1
        if self._link_failures >= self.LINK_FAILURES_BEFORE_RECOVERY:
            # Reset the count before recovering, not after: a link that is still dead then gets
            # a full run of failures before the next attempt, instead of rebooting the board on
            # every verb for as long as it stays down.
            self._link_failures = 0
            try:
                self.link_recovered = self.recover_link()
            except Exception as e:
                # Recovery is best-effort by construction. A Hub with several other endpoints
                # to serve must not go down because one adapter could not be rebooted.
                if self.debug:
                    print("Link recovery raised: {}".format(e))
                self.link_recovered = False
        return None

    def recover_link(self):
        """Bring a stalled link back, if this tunnel knows how. False when it does not.

        The base tunnel does not: a WiFi link has no board to reboot and no descriptor to
        rebuild, so the honest answer is that nothing was recovered. `Serial_connector`
        overrides it, because a serial adapter is a board on a wire that can be power-cycled
        and reopened.
        """
        return False

    # --- transport verbs, forwarded to the bridge radio over the link ---

    def transmit(self, wire):
        rid = self._next_req_id()
        reply = self._rpc(tunnel_codec.encode_transmit(wire, req_id=rid),
                          timeout=self._rpc_timeout, req_id=rid)
        return tunnel_codec.decode_bool_reply(reply) if reply else False

    def listen(self, window):
        rid = self._next_req_id()
        reply = self._rpc(tunnel_codec.encode_listen(window, req_id=rid),
                          timeout=window + self._link_margin, req_id=rid)
        if not reply:
            self._remember_signal(None, None)
            return None, window          # link gave up: treat as a radio timeout of ~one window
        wire, td, rssi, snr = tunnel_codec.decode_listen_reply(reply)
        self._remember_signal(rssi, snr)
        # Two values out, as every Connector's listen returns: the readings reached this half
        # through the reply, but they are read back through get_rssi/get_snr like any radio's.
        return wire, td

    def recv(self, focus_time=12):
        return self.listen(focus_time)[0]

    def exchange(self, wire, window, match_key):
        # The bridge runs the whole match loop at its radio; we send only the keyless prefix
        # down and get the matching reply (or a timeout) + the radio-measured td back up.
        prefix = match_key.wire_prefix()
        rid = self._next_req_id()
        reply = self._rpc(tunnel_codec.encode_exchange(wire, window, prefix, req_id=rid),
                          timeout=window + self._link_margin, req_id=rid)
        if not reply:
            self._remember_signal(None, None)
            return None, window, "timeout"
        got, td, status, rssi, snr = tunnel_codec.decode_exchange_reply(reply)
        self._remember_signal(rssi, snr)
        return got, td, status

    # --- what the bridge's radio heard ---

    def _remember_signal(self, rssi, snr):
        self._last_rssi = rssi
        self._last_snr = snr

    def get_rssi(self):
        """The RSSI of the last frame the BRIDGE's radio received, or None when there was none.

        The base Connector's stub returns 0 for a connector with no radio to ask, and a tunnel
        has none on this side, so every Hub on a USB adapter reported 0 dBm for hardware it was
        hearing perfectly. The number was never missing, only stranded: the bridge measures it
        on arrival and now carries it home in the reply.

        None, not 0, when nothing was heard. 0 dBm is a reading no LoRa radio ever takes, so a
        consumer that cannot tell it from a real one will draw a link at full strength for a
        node that has said nothing.
        """
        return self._last_rssi

    def get_snr(self):
        """The SNR of the last frame the bridge's radio received, or None. See `get_rssi`; the
        one difference is that 0 dB SNR is an ordinary reading, so only None can mean absent."""
        return self._last_snr

    def send_and_wait_response(self, packet):
        """The initiator round over the tunnel: frame here, match at the radio via `exchange`,
        deframe the returned reply here. Same (response | error-dict, size_sent, size_recv, td)
        contract the engine reads for a local radio, so Hub/Edge are untouched."""
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
        # the final say: a right-prefix-but-corrupt or unverifiable frame deframes to None.
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
        rid = self._next_req_id()
        reply = self._rpc(
            tunnel_codec.encode_set_rf(freq=frequency, sf=sf, bw=bw, cr=cr, tx=tx_power,
                                       req_id=rid),
            timeout=self._rpc_timeout, req_id=rid)
        if not reply or not tunnel_codec.decode_bool_reply(reply):
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
        """The bridge radio's RF config, falling back to this half's own configured values.

        Those values are not a guess: `config()` set them from the same file that decided what
        to ask the bridge for, `change_rf_config` keeps them in step, and they are precisely
        what the base Connector returns for a node whose radio is local. Standing in with them
        is therefore the same answer, sourced from this side of the link.

        The fallback covers an answer that is absent AND one that is unusable, because the two
        arrive differently. A bridge still finishing a previous client's `listen` or `exchange`
        replies to *that* verb, and it does so after this half has already flushed the link, so
        no flush can catch it. That reply is well-formed and carries no `rf`, so decoding it
        yields None rather than nothing at all.

        Returning None or an empty list here is what makes that a lasting fault instead of a
        missed round trip: a node snapshots this once, at construction, and resolves every
        endpoint's RF against the snapshot for as long as it runs. One unlucky reply at startup
        would leave it registering endpoints it can never visit.
        """
        rid = self._next_req_id()
        reply = self._rpc(tunnel_codec.encode_get_rf(req_id=rid), timeout=self._rpc_timeout,
                          req_id=rid)
        if reply:
            rf = tunnel_codec.decode_get_rf_reply(reply)
            if rf:
                return rf
        return super().get_rf_config()

    def request_mac(self, retries=3):
        """Fetch the bridge radio's MAC (the real on-air address the peer answers to), cache it
        as this connector's identity, and rebuild the codec so its reply-matching uses it. The
        bridge may still be booting when a tunnel Collector comes up, so retry a few times.

        The retries are also what makes the id enough here. This runs at `config()`, the moment
        a bridge is most likely to still be finishing something for whoever held the link
        before, so a reply that answers an earlier call is thrown away and this simply asks
        again rather than adopting a MAC that was never sent.
        """
        for _ in range(max(1, retries)):
            rid = self._next_req_id()
            reply = self.link.rpc(tunnel_codec.encode_get_mac(req_id=rid),
                                  timeout=self._rpc_timeout)
            if reply and not self._answers(reply, rid):
                continue
            if reply:
                mac = tunnel_codec.decode_get_mac_reply(reply)
                if mac and mac != self.MAC:
                    self.MAC = mac
                    self._rebuild_codec()
                if mac:
                    return self.MAC
        return self.MAC

    def _rebuild_codec(self):
        # Re-frame the codec with the now-known MAC. This is the open-mode rebuild; MAC
        # addressing is v2 (no secure posture), and the v3 secure path is sid/device_id-addressed
        # (MAC-independent), so it never reaches here with a secure codec to preserve.
        self.codec = build_codec(
            protocol_version=self.protocol_version, addressing=self.addressing,
            mesh_mode=self.mesh_mode, short_mac=self.short_mac, my_mac=self.get_mac())

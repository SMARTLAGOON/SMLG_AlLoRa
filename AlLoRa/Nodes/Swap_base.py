"""The unified swappable node: one object carrying BOTH whole-file loops.

A transfer always has a *drive* side (the initiator: polls, asks METADATA/CHUNK, reassembles)
and a *serve* side (the responder: lives with the data, answers each request). Historically
those were two classes (Requester drives, Source serves), which made role reversal need two
mirrored node instances copying state between them, the chief fragility of the old role-swap
experiment. Here both loops live on one base with one config, one connector and one session
state; `current_role` ("collector" drives, "source" serves) picks which loop runs, so
reversing a role never constructs, mirrors or synchronizes a second node.

The presets pick a home:  an Edge serves by default, a Hub drives by default. No swap state is
ever persisted. On boot a node is back at `home_role`, and the Hub resuming its poll
re-converges the pair after any failure.
"""
import gc
from os import urandom
from AlLoRa.Nodes.Node import Node, Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.File import AlLoRa_File
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Pacing import Pacing
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep, sleep_ms, \
    ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os


class Swap_base(Node):

    def __init__(self, connector=None, config_file="LoRa.json",
                 debug_hops=False,
                 max_sleep_time=3,
                 successful_interactions_required=5,
                 data_sink=None,
                 datasource=None,
                 home_role="source"):
        super().__init__(connector, config_file)
        gc.enable()

        # The role dispatch: "source" runs the serve loop, "collector" the drive loop.
        # current_role is RAM-only on purpose: a reboot must land on home_role.
        self.home_role = home_role
        self.current_role = home_role

        # --- serve-side state (the node as data holder) --------------------------------
        max_chunk_size = self.calculate_max_chunk_size()
        if self.chunk_size > max_chunk_size:
            self.chunk_size = max_chunk_size
            if self.debug:
                print("Chunk size too big, setting to max: ", self.chunk_size)
        self.file = None

        # The serve side's input boundary, the mirror of data_sink: with a datasource
        # attached, the serve loop pumps it (non-blocking) and installs its next pending
        # file whenever the node is idle. Without one, files arrive via set_file as ever.
        self.datasource = datasource
        self._datasource_ready = False

        # --- drive-side state (the node as poller/reassembler) -------------------------
        self.debug_hops = debug_hops

        # The inter-request sleep controller lives in Pacing (policy on the logic-holder,
        # mirroring the adaptive-window extraction). Pacing is fed the sf/bw-derived bounds;
        # the init cap is the caller's `max_sleep_time`, not the ToA-derived max, preserving
        # the original two-step init, where calculate_sleep_time_bounds' max was overwritten.
        min_sleep, _ = self.calculate_sleep_time_bounds()
        self.pacing = Pacing(successful_interactions_required=successful_interactions_required,
                             max_failures=3, exponential_backoff_threshold=0.5)
        self.pacing.set_sleep_bounds(min_sleep, max_sleep_time)

        self.result_path = "Results"
        if self.config:
            self.result_path = self.config.get('result_path', "Results")
            if self.debug:
                print("Result path: ", self.result_path)

        # The completed-file output boundary, symmetric to DataSource on the serve side. The
        # default (persist to Results/<source>/ exactly as before) is built lazily on first
        # drive, so a node that only ever serves never touches the filesystem for it. On an
        # Edge this sink is where a *downlink* lands. Its drive loop only ever pulls from
        # its Hub.
        self.data_sink = data_sink
        self._drive_ready = False

        self.status["SMAC"] = "-"   # peer MAC while driving
        self.source_mac = None
        self.time_request = time()
        self._wire_chunk_size = None   # v3: sender's chunk_size, read from typed METADATA

        # While serving a delegated downlink, requests arrive under the *session's* sid
        # (the Edge's), not this node's own; is_for_me accepts that one sid for the duration.
        self._delegated_sid = None

        # Hub-authority tie-break bookkeeping: the kind of the last frame that landed in the
        # reply slot, and how often this node yielded a delegated drive to the authority.
        self._last_reply_kind = None
        self.yield_count = 0

    def is_for_me(self, packet):
        if super().is_for_me(packet):
            return True
        return (self._delegated_sid is not None
                and self.protocol_version >= 3
                and packet.addressing == "sid"
                and packet.get_session() == self._delegated_sid)

    def _prepare_drive(self):
        # First-drive setup: the Results dir must exist before a reassembly buffer opens its
        # temp file under it, and a node with no injected sink gets the disk default.
        if self._drive_ready:
            return
        try:
            os.mkdir(self.result_path)
        except Exception as e:
            if self.debug:
                print("Error creating result path: {}".format(e))
        if self.data_sink is None:
            from AlLoRa.DataSinks.Disk_DataSink import Disk_DataSink
            self.data_sink = Disk_DataSink(self.result_path)
        self._drive_ready = True

    # `NEXT_ACTION_TIME_SLEEP` now lives in `Pacing.sleep`; this property keeps every call
    # site working (the loop's `finally`, `Gateway.check_digital_endpoints`, examples).
    @property
    def NEXT_ACTION_TIME_SLEEP(self):
        return self.pacing.sleep

    @NEXT_ACTION_TIME_SLEEP.setter
    def NEXT_ACTION_TIME_SLEEP(self, value):
        self.pacing.sleep = value

    # =====================================================================================
    # Serve side. The responder loop: hold a file, answer each request for it.
    # =====================================================================================

    def get_chunk_size(self):
        return self.chunk_size

    def got_file(self):     # Check if I have a file to send
        return self.file is not None

    def set_file(self, file: AlLoRa_File):
        # Installing a file to serve starts a fresh delivery, even if this same
        # object was already served once (re-queued downlink, broadcast).
        file.reset_delivery()
        self.file = file

    def restore_file(self, file: AlLoRa_File):
        self.set_file(file)
        self.file.first_sent = time()
        self.file.metadata_sent = True

    def _pump_datasource(self):
        # One cooperative round of the input boundary, from inside the serve loop: it
        # shares the radio loop, so check() must never block. The queue's pop is the
        # handover. Once installed, the file is the node's to serve to completion (the
        # RAM queue wouldn't survive a reboot anyway, so peek-retain buys nothing here).
        if self.datasource is None:
            return
        if not self._datasource_ready:
            self.datasource.prepare()
            self._datasource_ready = True
        self.datasource.check()
        if self.file is None and self.datasource.has_pending():
            self.set_file(self.datasource.get_next_file())

    def establish_connection(self, try_for=None):
        while True:
            if self.debug:
                print("Establish")
            new_sf = None
            packet = self.listen_requester()
            if packet:
                if self.is_for_me(packet):
                    command = packet.get_command()
                    if Packet.check_command(command):
                        if command != Packet.OK:
                            return True
                        response_packet = Packet(self.mesh_mode, self.short_mac)
                        response_packet.set_source(self.MAC)
                        response_packet.set_destination(packet.get_source())
                        response_packet.set_ok()

                        if packet.get_change_rf():
                            new_sf = packet.get_config()
                            response_packet.set_change_rf(new_sf)
                        if self.mesh_mode and packet.get_mesh() and packet.get_hop():
                            response_packet.enable_mesh()
                            if not packet.get_sleep():
                                response_packet.disable_sleep()
                        if packet.get_debug_hops():
                            response_packet.add_previous_hops(packet.get_message_path())
                            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)

                        self.send_response(response_packet)

                        if self.subscribers:
                            self.status['Status'] = 'OK'
                            self.notify_subscribers()

                        if new_sf:
                            response_packet.set_change_rf(new_sf)
                            self.change_rf_config(new_sf)

                        return False
                else:
                    if self.debug:
                        print("Not for me, my mac is: ", self.MAC, " and packet mac is: ", packet.get_destination())
                    self.forward(packet)
            gc.collect()
            if try_for is not None:
                try_for -= 1
                if try_for <= 0:
                    return False

    def answer_handshake(self, request):
        """As the serve side (handshake initiator), answer the drive side's handshake CTRL: on
        INIT, make a fresh ephemeral key and return the HELLO (its public key); on WELCOME,
        derive + store the session and return the ACK. Returns the reply packet, or None on an
        unexpected message. A fresh ephemeral key per session means a reboot re-handshakes."""
        from AlLoRa.Security.handshake import initiator_hello, initiator_complete
        payload = request.get_payload()
        # Answer in the same addressing the poll used: under my own device_id[:4] (v3), or
        # mirrored back to the poller's MAC (the retiring legacy shape).
        if request.addressing == "did":
            addressing, token = "did", self.device_id[:4]
        else:
            addressing, token = "mac", request.get_source()
        if payload and payload[0] == Node._HS_INIT:
            # A repeated INIT (our HELLO was lost) just makes a fresh ephemeral. The latest
            # one is what the poller will accept, so the two ends stay in step.
            self._hs_state, hello = initiator_hello(urandom)
            return self._ctrl_packet(token, Node._HS_HELLO, hello, addressing=addressing)
        if payload and payload[0] == Node._HS_WELCOME:
            if self._hs_state is not None:
                # If the WELCOME shed its sid (the common case), fall back to the sid both ends
                # derive from my identity, device_id[0].
                session = initiator_complete(self._hs_state, payload[1:],
                                             default_sid=self.device_id[0])
                self.session_store.put(session)
                # Follow the established session's sid into the data phase: the peer may
                # have reassigned it off device_id[0] to break a clash, and my data frames must
                # carry the same sid the session was keyed under.
                self.session_id = session.sid
                self._hs_state = None
            # If the state is already cleared, a prior WELCOME completed and its ACK was lost;
            # re-ACK idempotently so the peer's retransmit still lands.
            return self._ctrl_packet(token, Node._HS_ACK, addressing=addressing)
        return None

    def _handshake_responder(self, request):
        # respond()-compatible handler: build the handshake reply and send it.
        self.send_response(self.answer_handshake(request))

    def _respond_handler(self, packet):
        # During a secure transfer the serve side may still get a first-contact handshake CTRL
        # (e.g. the peer re-handshaking); route those to the handshake, data to serving.
        kind = packet.get_command()
        if kind == Packet_v3.GRANT:
            # A delegation from the authority. Never answered on the wire: the granted pull
            # itself is the acknowledgement, and only an Edge preset honors it.
            self._on_grant(packet)
            return
        if kind == Packet_v3.CTRL:
            self.send_response(self.answer_handshake(packet))
        else:
            self._serve(packet)

    def _on_grant(self, packet):
        # Base: ignore. The Edge preset overrides this to accept the delegated drive role;
        # a Hub (the authority) never takes a GRANT from anyone.
        pass

    def _service_grant(self):
        # Base: nothing to honor. The Edge preset overrides this to run a pending
        # delegated pull. Called from every serve wait loop (`serve()` AND the legacy
        # `send_file()` main loops fielded firmware runs), so a delegation reaches an
        # Edge no matter which loop it lives in.
        pass

    def _maybe_delegate(self, digital_endpoint):
        # Base: nothing to delegate. The Hub preset overrides this to hand the drive role
        # to an Edge (GRANT) when a downlink file is pending for it at a safe boundary.
        return False

    def _heard_authority_poll(self):
        # Hub-authority tie-break. Only a *delegated* drive (an Edge granted the collector
        # role) can hear this: during its pull the only OK-kind frame that can land in the
        # reply slot is the authority polling again: it reclaimed, so the delegation is
        # over. The Edge yields instantly and goes home; the abandoned pull simply re-runs
        # on a later GRANT with a fresh buffer. A permanent authority never yields.
        if (self.home_role == "source" and self.current_role == "collector"
                and self._last_reply_kind == Packet_v3.OK):
            self.yield_count += 1
            if self.debug:
                print("Yielding the drive role: the authority is polling again")
            return True
        return False

    def _serve(self, packet):
        # The serve side's responder handler: build the reply for this request, send it, and
        # apply any accepted RF change (after the confirming reply is on the wire).
        response_packet, new_sf = self.response(packet)
        self.send_response(response_packet)
        if new_sf:
            backup_cks = self.chunk_size
            self.change_rf_config(new_sf)
            # Idle serving with no file is legal (an Edge between transfers): the new
            # chunk size then only lands on self.chunk_size, for the next set_file.
            if self.chunk_size != backup_cks and self.file is not None:
                self.file.change_chunk_size(self.chunk_size)

    def send_file(self, timeout=float('inf')):
        t0 = time() # Start time in ms
        while not self.file.sent:
            packet = self.respond(self._respond_handler)
            self._service_grant()
            if packet is None and self.sf_trial:
                self.sf_trial -= 1
                if self.sf_trial <= 0:
                    self.restore_rf_config()
                    self.sf_trial = False

            if ticks_diff(time(), t0) > timeout:
                last_sent = self.file.last_chunk_sent
                del(self.file)
                gc.collect()
                self.file = None
                if self.debug:
                    print("Timeout reached")
                # If something was sent, but not all, we return a True
                # (chunk indexes are 0-based, so "chunk 0 was sent" must count as sent)
                if last_sent is not None:
                    return True
                return False

        del(self.file)
        gc.collect()
        self.file = None
        return True

    def response(self, packet):
        command = packet.get_command()
        if not Packet.check_command(command):
            return None, None

        v3 = self.protocol_version >= 3
        response_packet = self.new_packet()
        if not v3:
            response_packet.set_source(self.MAC)
            response_packet.set_destination(packet.get_source())
        elif packet.addressing == "sid":
            # Reply under the *session's* sid: mirror the request. In the home direction
            # they coincide; while serving a delegated downlink the session is addressed by
            # the Edge's sid, not this node's own.
            response_packet.set_session(packet.get_session())

        if self.mesh_mode:
            if packet.get_mesh() and packet.get_hop():
                response_packet.enable_mesh()
                if not packet.get_sleep():
                    response_packet.disable_sleep()

        new_sf = None
        if self.sf_trial:
            if self.debug:
                print("SF Trial ended successfully")
            self.sf_trial = False
            self.backup_config()

        if not v3 and packet.get_debug_hops():
            response_packet.set_data("")
            response_packet.enable_debug_hops()
            response_packet.add_previous_hops(packet.get_message_path())
            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)
            return response_packet, new_sf

        if command == Packet.CHUNK:
            if self.file is None:
                # Nothing to serve: stay silent like a node that isn't serving yet. The
                # poller times out and retries, exactly the pre-transfer behavior.
                return None, new_sf
            requested_chunk = packet.get_chunk_index() if v3 else int(packet.get_payload().decode())
            response_packet.set_data(self.file.get_chunk(requested_chunk))
            if self.subscribers:
                self.status['Chunk'] = self.file.get_length() - requested_chunk
                self.status['Status'] = 'CHUNK'
                self.status['Retransmission'] = self.file.retransmission

            if self.debug:
                print("RC: {} / {}".format(requested_chunk, self.file.get_length()))

            if not self.file.first_sent:
                self.file.report_SST(True)
            return response_packet, new_sf

        if command == Packet.METADATA:    # handle for new file
            if self.file is None:
                return None, new_sf
            filename = self.file.get_name()
            if v3:
                # Typed METADATA: carry chunk_size + total byte length so the receiver's
                # positioned writes stop depending on both ends being configured with the
                # same chunk_size. v2 sent only chunk_count + filename.
                response_packet.set_metadata(self.file.chunk_size, self.file.length, filename)
            else:
                response_packet.set_metadata(self.file.get_length(), filename)

            if self.file.metadata_sent:
                self.file.retransmission += 1
                if self.debug:
                    print("Asked again for Metadata...")
            else:
                self.file.metadata_sent = True

            if self.subscribers:
                self.status['File'] = filename
                self.status['Status'] = 'Metadata'
                self.status['Chunk'] = self.file.get_length()
                self.status['Retransmission'] = self.file.retransmission
            return response_packet, new_sf

        if command == Packet.OK:
            response_packet.set_ok()

            if (not v3) and packet.get_change_rf():
                new_sf = packet.get_config()
                response_packet.set_change_rf(new_sf)
            elif self.file is not None and self.file.first_sent and not self.file.last_sent:
                if not v3:
                    # Legacy shape, byte-for-byte: any mid-transfer OK finalizes AND is
                    # answered: a v2 requester listens for this reply on its connection
                    # poll, so suppressing it would time that poll out.
                    self.file.sent_ok()
                elif self.file.last_chunk_sent == self.file.get_length() - 1:
                    # Only an OK arriving after the tail chunk was served can be the
                    # initiator's fire-and-forget final-OK: it ends the transfer and
                    # nobody listens for a reply, so answering would only burn airtime.
                    # An OK any earlier is a connection poll (e.g. the authority
                    # re-polling after a reboot) and gets its keepalive answer below:
                    # a half-sent file must never be marked sent by a poll.
                    self.file.sent_ok()
                    return None, new_sf
            return response_packet, new_sf

        return response_packet, new_sf

    # =====================================================================================
    # Drive side. The initiator loop: poll a peer, pull METADATA + CHUNKs, reassemble.
    # =====================================================================================

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

    def send_request(self, packet: Packet) -> Packet:
        if self.mesh_mode and self.protocol_version < 3:   # v3 mesh uses seq, not a random id
            packet.set_id(self.generate_id())
            if self.debug_hops:
                packet.enable_debug_hops()

        self.time_since_last_request = ticks_diff(time(), self.time_request)
        self.time_request = time()

        # One initiator round via the shared verb (wraps the connector's send_and_wait_response).
        response_packet, packet_size_sent, packet_size_received, time_pr = self.request(packet)

        # Remember what kind landed in the reply slot (None on timeout/error): a delegated
        # drive uses it to hear the authority's contending poll and yield.
        if response_packet is None or isinstance(response_packet, dict):
            self._last_reply_kind = None
        else:
            self._last_reply_kind = response_packet.get_command()

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

    def perform_handshake(self, digital_endpoint, tries=5):
        """As the drive side (the handshake responder + sid-assigner), drive the two-round ECDH
        with a serve-side peer over open MAC CTRL frames and store the resulting Session. Each
        round is retried up to `tries` times so a dropped handshake frame recovers (a retried
        INIT gets a fresh ephemeral; a retried WELCOME gets an idempotent re-ACK). Returns the
        Session, or None if a round never lands. The peer authenticates nothing here: in
        `secure` this is confidentiality-only; the gateway accepts the peer by its registered
        identity, and the catastrophic downlink is guarded separately."""
        from AlLoRa.Security.handshake import responder_accept
        from AlLoRa.Security.ec_p256 import generate_private_key
        if self.static_priv is None:
            self.static_priv = generate_private_key(urandom)

        # Address follows registration: a device_id-registered peer takes the v3 did-addressed
        # path (no MAC on the wire); a MAC-registered one keeps the legacy two-MAC handshake.
        did = digital_endpoint.get_did()
        if did is not None:
            addressing, token = "did", did
            # The sid rides the WELCOME only when we had to move it off the derived value to
            # break a clash. Otherwise both ends already derive device_id[0].
            send_sid = digital_endpoint.session_id != digital_endpoint.derived_sid()
        else:
            addressing, token = "mac", digital_endpoint.get_mac_address()
            send_sid = True    # no shared identity to derive the sid from; it must be sent
        sid = digital_endpoint.session_id

        # round 1: prompt the peer for its ephemeral public key
        hello = None
        for _ in range(tries):
            hello = self.send_request(self._ctrl_packet(token, Node._HS_INIT, addressing=addressing))
            if self._is_hs(hello, Node._HS_HELLO):
                break
        if not self._is_hs(hello, Node._HS_HELLO):
            return None
        session, welcome = responder_accept(self.static_priv, hello.get_payload()[1:], sid,
                                            send_sid=send_sid)

        # round 2: send our static public key (+ sid only on reassignment), expect the ack
        for _ in range(tries):
            if self._is_hs(self.send_request(
                    self._ctrl_packet(token, Node._HS_WELCOME, welcome, addressing=addressing)),
                    Node._HS_ACK):
                self.session_store.put(session)
                return session
        return None

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

        self._prepare_drive()

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

        # Secure first contact: establish a session (ECDH handshake) before the transfer, once
        # per session lifetime. The RAM store keeps it across subsequent listens. If it fails
        # there is nothing to protect the transfer with, so give up this endpoint for now.
        if self.security_mode == 'secure' and self.session_store is not None \
                and self.session_store.get(digital_endpoint.session_id) is None:
            if self.perform_handshake(digital_endpoint) is None:
                if self.debug:
                    print("Handshake failed with endpoint: ", mac)
                return False

        t0 = time()
        if listening_time is None or listening_time == float('inf'):
            end_time = None    # listen forever (a wrapped deadline can't express it)
        else:
            end_time = ticks_add(t0, listening_time * 1000)

        while end_time is None or ticks_diff(end_time, time()) > 0:
            t0 = time()
            delegated = False

            try:
                packet_request = self.create_request(mac, digital_endpoint.get_mesh(), sleep_mesh,
                                                     digital_endpoint.session_id)

                # The safe boundary for a downlink delegation: any idle point between
                # complete files (pre-contact OK, or the idle metadata-poll loop), but
                # never mid-chunk (a reassembly in progress must finish first). With a
                # downlink pending, the Hub preset delegates the drive role here (GRANT +
                # serve + reclaim) instead of running this round's request.
                if digital_endpoint.state != "PROCESS_CHUNK_STATE" \
                        and self._maybe_delegate(digital_endpoint):
                    delegated = True
                    t0 = time()

                elif digital_endpoint.state == "REQUEST_DATA_STATE":
                    if self.debug:
                        print("ASKING METADATA to {}".format(mac))
                    metadata, hop = self.ask_metadata(packet_request)
                    t0 = time()
                    if self._heard_authority_poll():
                        return False
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
                        if self._heard_authority_poll():
                            return False
                        self.status['Chunk'] = digital_endpoint.file_reception_info["total_chunks"] - next_chunk
                        file = digital_endpoint.set_data(data, hop, self.mesh_mode)
                        if file:
                            # The sink is fed BEFORE the fire-and-forget final-OK: that OK
                            # retires the file on the serving side (and pops a delegated
                            # downlink off its queue), so a sink failure after it would
                            # lose the file with no retry left anywhere. Failing here
                            # leaves the transfer unacknowledged and the next round pulls
                            # the file again. Delivery is at-least-once, and a lost
                            # final-OK may feed an (idempotent) sink twice.
                            if print_file:
                                print(file.get_content())
                            if save_file:
                                try:
                                    self.data_sink.consume(
                                        file, self._reception_context(digital_endpoint, mac))
                                except Exception:
                                    # No ack for an unconsumed file, and no OK poll
                                    # either: to a peer whose last chunk went out, an OK
                                    # is exactly the final-OK and would retire the file.
                                    # Rewind to re-pull it whole next round instead.
                                    file.discard()
                                    digital_endpoint.state = Digital_Endpoint.REQUEST_DATA_STATE
                                    raise
                            final_ok = self.create_request(mac, digital_endpoint.get_mesh(),
                                                           sleep_mesh, digital_endpoint.session_id)
                            final_ok.set_ok()
                            if self.protocol_version < 3:
                                final_ok.set_source(self.connector.get_mac())
                            sleep(1)
                            self.send_lora(final_ok)
                            self.status['Chunk'] = "DONE"
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

                if not delegated:
                    # A delegation round served nobody a request: it says nothing about
                    # the inter-request gap this link tolerates, so it must not feed
                    # the sleep controller's hunt.
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

                dt = ticks_diff(time(), t0) / 1000

                self.pacing.on_failure()

            finally:
                if self.subscribers:
                    self.status['Status'] = digital_endpoint.state
                    self.notify_subscribers()

                gc.collect()
                dt = ticks_diff(time(), t0) / 1000
                if self.debug:
                    print("DT: ", dt, "Sleep time: ", self.NEXT_ACTION_TIME_SLEEP)
                sleep_time = self.pacing.next_sleep()
                if self.debug:
                    print("Sleep time: ", sleep_time)
                if sleep_time > 0:
                    sleep(sleep_time)

            # Outside the finally on purpose: a `break` inside it silently discards
            # any exception still propagating from the try/except (and is a hard
            # SyntaxError on newer Pythons). The one-file drive sets `stop` in the
            # try with no exception in flight, so breaking here, after the finally's
            # notify/gc/sleep cleanup, is behavior-identical and safe.
            if stop:
                break

        # True only when a one_file drive completed (its file reached the sink):
        # a granted pull uses this to tell a delivered delegation from a dead one.
        return stop

    def _reception_context(self, digital_endpoint, mac):
        # Freeze a completion record for the sink: identity + a final RF/quality snapshot, taken
        # now (the endpoint is reused for the next file). Every field is best-effort: a missing
        # RSSI/did must never break delivery, so each lookup is guarded.
        from AlLoRa.DataSinks.DataSink import Reception
        did = None
        try:
            did = digital_endpoint.get_did()
        except Exception:
            pass
        rssi = snr = None
        try:
            rssi = self.connector.get_rssi()
        except Exception:
            pass
        try:
            snr = self.connector.get_snr()
        except Exception:
            pass
        total_chunks = None
        info = getattr(digital_endpoint, "file_reception_info", None)
        if isinstance(info, dict):
            total_chunks = info.get("total_chunks")
        return Reception(source=mac, session_id=digital_endpoint.session_id,
                         device_id=did, rssi=rssi, snr=snr,
                         total_chunks=total_chunks, timestamp_ms=time())

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
        if self.debug:
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

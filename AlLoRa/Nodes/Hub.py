"""Hub: the center-placement authority (formerly `Requester`/`Collector`/`Gateway`).

Named by where it sits: the Hub is the permanent controller of its Edges. It polls, pulls
their uplink files, and never surrenders control. Its home role is "collector" (the drive
loop); it also carries the serve loop because a downlink temporarily reverses the roles:
the Hub delegates the collector role to an Edge with a GRANT, serves the pending file to the
Edge's pull, and reclaims control the moment the pull ends (or its reclaim timer fires).

A Hub holds one or more endpoints and runs the visit loop over them itself (`run()`). One
endpoint is the 1:1 collector, many is the gateway deployment: the same class either way,
which is why the two v2 classes it replaces are gone rather than kept as separate nodes.
"""
import gc
from os import urandom
from json import loads, dumps
from AlLoRa.Nodes.Node import Node
from AlLoRa.Digital_Endpoint import Digital_Endpoint, assign_session_ids, \
    label_for_config, NO_ADDRESS
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.Control.control_types import RF_CONFIG, IN_BAND
from AlLoRa.File import AlLoRa_File
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.utils.time_utils import current_time_ms as time, sleep, ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print


class _Downlink_queue(DataSource):
    """The Hub's default per-Edge downlink source: a plain FIFO behind the DataSource
    seam. Unlike the base queue it neither dedupes by name nor evicts on overflow: a
    command queued twice must be delivered twice, and dropping a queued control file
    to make room would be silent data loss on the catastrophic direction."""

    def add_to_queue(self, file):
        self.file_queue.append(file)


class Hub(Node):

    # Rounds an in-band control command is retried before the call gives up. Matched to the
    # legacy in-band reconfiguration rather than tuned: on a lossy link a single dropped frame
    # must not read as a refused change, and a reconfiguration is rare enough that spending a
    # few rounds on it costs nothing the transfer loop will miss.
    _IN_BAND_ATTEMPTS = 20

    # Connection polls spent telling a peer that declined a command from a peer that is not
    # there. More than one because the attempts above prove nothing about the return path: a
    # node that refuses hears every one of them and answers none, by design, so the first frame
    # that tests whether anything can come back at all is this poll. Resting the whole
    # diagnosis on a single frame over a lossy radio would report a refusing node as a dead one
    # every time the link dropped one short packet, which is the wrong answer this exists to
    # stop giving. Three, and it stops at the first reply: it costs nothing on the path that
    # succeeds and three short frames on the path that has already spent twenty.
    _DIAGNOSIS_POLLS = 3

    # The Hub is the authority: it issues control artifacts and is never commanded by one over
    # the radio. So the half of the control root it may hold is the signing half, and the
    # verifying half, which it could do nothing with, is refused as a misprovisioning.
    _MINTS_CONTROL = True

    def __init__(self, connector=None, config_file="LoRa.json",
                 debug_hops=False,
                 max_sleep_time=3,
                 successful_interactions_required=5,
                 data_sink=None,
                 nodes_file="Nodes.json",
                 reclaim_timeout=10,
                 probe_swap_after=1,
                 probe_give_up_after=6,
                 session_recovery_after=3,
                 control_root=None,
                 control_counter_file=None,
                 control_actuator=None):
        super().__init__(connector, config_file,
                         debug_hops=debug_hops,
                         max_sleep_time=max_sleep_time,
                         successful_interactions_required=successful_interactions_required,
                         data_sink=data_sink,
                         control_actuator=control_actuator,
                         home_role="collector")
        # The endpoints this Hub polls, registered from a Nodes.json-shaped file. Registration
        # fails soft: a Hub with none is a node with nothing to poll, not a boot failure.
        self.nodes_file = nodes_file
        self.digital_endpoints = []
        self.status["Digital_Endpoints"] = {}
        if nodes_file:
            self.add_digital_endpoints(nodes_file)
        # RF-config probe budgets (in visits): swap {new, old} after this many silent visits on
        # a config, and give up (restore to old) after this many silent visits in total. A
        # reconfig is rare and non-urgent, so one probe per visit keeps working Edges un-starved.
        self.probe_swap_after = probe_swap_after
        self.probe_give_up_after = probe_give_up_after
        # Downlink outbound: one DataSource per endpoint sid (a default FIFO queue, or a
        # live feed plugged in with set_downlink_source). A file stays the source's head
        # until an Edge's completed pull confirms delivery (peek-retain), so a failed
        # delegation simply retries at the next boundary.
        self.reclaim_timeout = reclaim_timeout
        self._downlink = {}
        # Rolling 1-byte GRANT counter, seeded randomly: the Edge remembers the last
        # id it completed, so a rebooted Hub restarting at a fixed value would have
        # its first GRANT silently dropped as a duplicate.
        self._swap_id = urandom(1)[0]
        # Per-sid [consecutive failed delegations, drive rounds left to skip]: an
        # undeliverable downlink (deaf Edge, dead channel) must back off and let the
        # uplink poll run, never turn every round into GRANT + reclaim silence.
        self._delegation_backoff = {}
        # RF_CONFIG mirroring. A reconfig downlink carries a mirror_config the backend minted
        # (the Hub never parses the signed envelope); after the Edge's final-OK confirms it, the
        # Hub switches its VIEW of that endpoint to the new config and enters a trial, keeping
        # the old config to probe {new, old} until the Edge is re-acquired. Per sid:
        #   _pending_mirror[sid] = the mirror_config awaiting this endpoint's next delivery.
        #   _endpoint_trial[sid] = {"old": [f,sf,bw,cr,txp], ...} while a trial is live.
        self._pending_mirror = {}
        self._endpoint_trial = {}
        # What became of the last RF change asked of each endpoint, per sid, so a caller can
        # still read the verdict after the call that started it has returned. It has to
        # outlive the call because on the signed route the verdict does not exist yet when the
        # call returns: the Edge decides alone, after the transfer closed, and the only thing
        # that can observe which way it went is the probe, visits later. Bounded by the
        # endpoint count, one short string each.
        self._rf_change_status = {}
        # Secure-session liveness, counted in visits. A peer that reboots loses its RAM-held
        # session and has no way to say so, and only this side can offer a new one, so the
        # authority has to notice by itself. How that is decided (and why silence alone does
        # not decide it) is in _session_visit_end. Per sid: (consecutive silent visits,
        # whether the confirming connection poll is already armed).
        self.session_recovery_after = session_recovery_after
        self._session_silent_visits = {}
        # The control root this Hub mints with, when it was provisioned with one, and the
        # counter its artifacts carry. ONE number for the whole fleet: every node compares only
        # against its own high-water mark, so a single increasing sequence satisfies all of them
        # at once and gaps in any one endpoint's view are normal and harmless.
        #
        # Both normally come from config, which is where a deployment declares its control
        # posture so it can be read off a node without running it. The constructor arguments
        # stay as an explicit override for a Hub assembled in code (a backend handing over a
        # root object it already holds, or a test), and an explicit one wins: it is a deliberate
        # act at the call site, while config is the standing declaration.
        if control_root is not None:
            self.control_root = control_root
        # Where the counter is kept comes from the node, which reads it off the same config key
        # an Edge does and defaults it the same way. That default matters most here: a node
        # remembers the highest number it has accepted whether or not anyone configured it to,
        # so the side issuing those numbers has to remember too. A Hub that began its sequence
        # again would be refused by every node it commands, and it could only work back through
        # the numbers it already spent one restart at a time, so it never catches up. The
        # recovery is rotating the root across the whole fleet, which is far too much to hang
        # on a line of config an operator has to know to write.
        #
        # Nothing is written where there is nothing to mint under: both the load and the save
        # do nothing unless this Hub holds a control root, so an open deployment never sees the
        # file. A filesystem that refuses the write degrades to RAM-only rather than failing
        # the command.
        if control_counter_file:
            self.control_counter_file = control_counter_file
        self._control_counter = self._load_control_counter()

    # --- the endpoints this Hub holds -----------------------------------------------------

    def add_digital_endpoints(self, path):
        """Register the active endpoints listed in a Nodes.json-shaped file.

        Returns how many endpoints the Hub holds afterwards, or False if the file could not
        be read (an absent or malformed file leaves the Hub running with nothing to poll)."""
        try:
            with open(path, "r") as f:
                nodes_config = loads(f.read())
            for node in nodes_config:
                if node['active']:
                    active_node = Digital_Endpoint(node)
                    self.digital_endpoints.append(active_node)
                    if self.debug:
                        print("Node {} ({}) added with frequency {}s and listening time {}s.".format(
                            active_node.get_name(), active_node.get_label(),
                            active_node.asking_frequency, active_node.listening_time))
            self._register_endpoints()
            return len(self.digital_endpoints)
        except Exception as e:
            if self.debug:
                print("Could not load nodes from file: {}, error: {}".format(path, e))
            return False

    def set_digital_endpoints(self, digital_endpoints):
        self.digital_endpoints = digital_endpoints
        self._register_endpoints()

    def _register_endpoints(self):
        # Give every endpoint a unique 1-byte sid, breaking any device_id[0] clash before
        # first contact (a Hub serving many Edges is where a clash can arise), then rebuild
        # the map subscribers read. Run on every change to the collection, so an endpoint
        # registered after boot is as addressable, and as visible, as one loaded from file.
        assign_session_ids(self.digital_endpoints)
        # Resolve RF here rather than at the first visit so the whole roster is decided, and
        # printed, at boot: which endpoints follow this Hub and which carry their own radio
        # is exactly what you want to read before wondering why one of them is silent.
        for endpoint in self.digital_endpoints:
            self.resolve_endpoint_rf(endpoint)
        self.status["Digital_Endpoints"] = {ep.get_label(): ep.file_reception_info
                                            for ep in self.digital_endpoints}

    def update_subscribers(self, digital_endpoint):
        self.status["Digital_Endpoints"][digital_endpoint.get_label()] = \
            digital_endpoint.file_reception_info
        self.status.notify()

    # --- the visit loop -------------------------------------------------------------------

    def run(self, timeout=None, print_file_content=False, save_files=False):
        """The Hub's main loop: visit each endpoint in turn, the most overdue one first.

        A visit is one listening window on that endpoint, extended once when it is locked on
        a file that still has chunks missing. `asking_frequency` (seconds, set per endpoint
        in Nodes.json) is how long before it comes up again. `timeout` is in seconds; None
        runs forever, which is what a deployed main.py wants.
        """
        print("Listening to {} endpoints!".format(len(self.digital_endpoints)))
        end_time = None if timeout is None else ticks_add(time(), timeout * 1000)
        # Everything is due on entry. Seeded with the current tick rather than 0 because
        # these are wrapping counters: a fixed 0 is not "the past", it is half a period away.
        # Keyed by label, never by MAC: device_id-registered endpoints all share the
        # "00000000" MAC default, which collapsed this whole map to ONE entry. Every
        # registered endpoint then shared a single due-time, so visiting any one of them
        # silenced all the others for a full asking_frequency and the round-robin died.
        next_visit = {ep.get_label(): time() for ep in self.digital_endpoints}

        while end_time is None or ticks_diff(end_time, time()) > 0:
            if not self.digital_endpoints:
                sleep(self.NEXT_ACTION_TIME_SLEEP)
                continue
            pass_start = time()
            for endpoint in sorted(self.digital_endpoints,
                                   key=lambda ep: ticks_diff(next_visit[ep.get_label()],
                                                             pass_start)):
                if end_time is not None and ticks_diff(end_time, time()) <= 0:
                    return
                label = endpoint.get_label()
                if ticks_diff(time(), next_visit[label]) >= 0:
                    try:
                        self._visit(endpoint, print_file_content, save_files)
                    except Exception as e:
                        if self.debug:
                            print("Error listening to endpoint {} ({}): {}".format(
                                endpoint.get_name(), label, e))
                    finally:
                        # Reschedule whether the visit worked or threw: an endpoint that
                        # fails every time must not be retried without pause, which would
                        # starve every other endpoint of the channel.
                        next_visit[label] = ticks_add(time(), endpoint.asking_frequency * 1000)
                sleep(self.NEXT_ACTION_TIME_SLEEP)

    def check_digital_endpoints(self, print_file_content=False, save_files=False, timeout=None):
        """Deprecated name for `run()`, kept so fielded main.py files call it unchanged.

        It described a check; what it does is run the node. New code says `Hub(...).run()`."""
        return self.run(timeout=timeout, print_file_content=print_file_content,
                        save_files=save_files)

    def _visit(self, digital_endpoint, print_file_content, save_files):
        if self.debug:
            print("Listening to endpoint {} ({}) for {}s".format(
                digital_endpoint.get_name(), digital_endpoint.get_label(),
                digital_endpoint.listening_time))
        self.listen_to_endpoint(digital_endpoint, digital_endpoint.listening_time,
                                print_file=print_file_content, save_file=save_files)
        self.update_subscribers(digital_endpoint)

        # A locked endpoint caught mid-file gets one extra window now, rather than holding
        # a half-received file for a whole asking_frequency before asking for the rest.
        if not digital_endpoint.lock_on_file_receive:
            return
        in_flight = digital_endpoint.get_current_file()
        if in_flight is None or not in_flight.get_missing_chunks():
            return
        if self.debug:
            print("Listening to endpoint {} ({}) for {}s due to missing chunks".format(
                digital_endpoint.get_name(), digital_endpoint.get_label(),
                digital_endpoint.max_listen_time_when_locked))
        self.listen_to_endpoint(digital_endpoint, digital_endpoint.max_listen_time_when_locked,
                                print_file=print_file_content, save_file=save_files)
        self.update_subscribers(digital_endpoint)

    def set_downlink_source(self, digital_endpoint, datasource):
        """Plug a live input boundary (e.g. an MQTT_DataSource) as this Edge's downlink:
        whatever it queues is delivered by delegation, exactly like queue_downlink files.
        Brought up here, at registration: a connect that must fail should fail at setup,
        loudly, not mid-drive-loop. Replaces the endpoint's previous source, so register
        before queueing anything."""
        datasource.prepare()
        self._downlink[digital_endpoint.session_id] = datasource

    def queue_downlink(self, digital_endpoint, file, mirror_config=None):
        """Queue an outbound file for one Edge. It is delivered by delegation: at the next
        safe boundary the drive loop sends GRANT and serves this file to the Edge's pull.

        `mirror_config` (a reconfig downlink) is the {freq, sf, bw, cr, tx_power, trial} the
        backend minted alongside the signed artifact. When this file is delivered, the Hub
        mirrors the endpoint to that config and enters a {new, old} probe trial — so the Hub
        follows the Edge onto the new radio parameters it is about to apply."""
        sid = digital_endpoint.session_id
        source = self._downlink.get(sid)
        if source is None:
            source = _Downlink_queue(self.chunk_size)
            self._downlink[sid] = source
        source.add_to_queue(file)
        if mirror_config is not None:
            self._pending_mirror[sid] = mirror_config

    def ask_change_rf(self, digital_endpoint, new_config, artifact=None):
        """Move one endpoint onto a new radio configuration, both ends, in one call.

        `new_config` is the {freq, sf, bw, cr, tx_power, trial} the Edge should apply. The
        transport is selected from what this Hub was provisioned with, never configured:

          * given the control root, the Hub mints the artifact itself;
          * given `artifact`, it carries what a backend minted, untouched;
          * given neither, the change goes in band on the link itself, as a control frame.

        The in-band route is selected by version as well: it speaks a v3 frame, so a v2 link
        falls through to the legacy encoding instead. Selecting rather than configuring is the
        point. A deployment does not choose how its control travels, it is told by what it was
        provisioned with, and a caller that could pick would be a caller that could pick wrong.

        Every route ends by moving this end onto `new_config` too: the signed ones through the
        mirror once delivery is confirmed, the in-band one as soon as the peer acknowledges. A
        reconfiguration where only the far end moves is not a partial success, it is an endpoint
        this Hub can no longer hear, so following is not left to the caller to remember.

        Returns one of ACCEPTED, REFUSED, PENDING or UNREACHABLE. Four words rather than a
        yes/no, because the two things a yes/no had to fuse are the two things a caller most
        needs kept apart. `True` used to mean "the peer acknowledged" in band and "an artifact
        is queued and nothing has happened yet" when signed, which is one value carrying a fact
        and a prediction; and `False` covered both a peer that declined the command and a peer
        that was not there, which are the same silence on the air and opposite repairs on the
        ground. `pending` is settled later by the probe, readable through `rf_change_status`.
        """
        if artifact is None and self.control_root is None:
            if self.protocol_version >= 3:
                outcome = self._ask_change_rf_in_band(digital_endpoint, new_config)
            else:
                # The v2 loop predates the vocabulary and answers yes or no, so translate here
                # rather than teach a legacy encoding a distinction it cannot draw. A v2 link
                # reaches only two of the four words, and that is honest: a v2 node holds no
                # control root and has no verify gate, so it has nothing to refuse a command
                # with. Its only failure is one nobody answered.
                outcome = self.ACCEPTED if super().ask_change_rf(digital_endpoint, new_config) \
                    else self.UNREACHABLE
            return self._record_rf_change(digital_endpoint, outcome)
        if artifact is None:
            payload = dumps(new_config).encode("utf-8")
            artifact = self.control_root.mint(RF_CONFIG, digital_endpoint.device_id,
                                              self._next_control_counter(), payload)
        file = AlLoRa_File(name="ctrl.bin", content=bytearray(artifact),
                           chunk_size=self.get_chunk_size())
        self.queue_downlink(digital_endpoint, file, mirror_config=new_config)
        # Queued, and nothing more can honestly be said yet. The Edge's verify gate runs after
        # the transfer has closed and this end has stopped listening, so the verdict is reached
        # somewhere this Hub cannot see it. The probe is what observes which way it went.
        return self._record_rf_change(digital_endpoint, self.PENDING)

    def rf_change_status(self, digital_endpoint):
        """What became of the last RF change asked of this endpoint: one of ACCEPTED, REFUSED,
        PENDING, UNREACHABLE, or None if none was ever asked.

        The reader for the half of the answer that does not exist when `ask_change_rf` returns.
        A signed change leaves here as PENDING and is settled by the probe on a later visit, so
        a backend or an operator screen watches this rather than the call's return value alone.
        """
        return self._rf_change_status.get(digital_endpoint.session_id)

    def _record_rf_change(self, digital_endpoint, outcome):
        self._rf_change_status[digital_endpoint.session_id] = outcome
        return outcome

    def _settle_rf_change(self, sid, outcome):
        # The probe only ever answers a question that is still open. A change the peer already
        # answered for itself is not re-judged by where its trial ended up: an in-band command
        # that was acknowledged WAS accepted, and an Edge whose trial later reverts is the
        # trial working, not the command being refused. Overwriting it would report a refusal
        # to whoever has to fix one, and send them to look at provisioning that is fine.
        if self._rf_change_status.get(sid) == self.PENDING:
            self._rf_change_status[sid] = outcome

    def _ask_change_rf_in_band(self, digital_endpoint, new_config):
        """Ask one Edge to retune over the link itself, with no envelope around the command.

        The frame *is* the command: a CTRL packet whose payload is one prefix byte (the in-band
        namespace bit ORed with the control type) followed by the same JSON the signed envelope
        carries. Both transports therefore reach the peer's actuator with identical arguments,
        and the actuator never learns how the command arrived.

        Addressing is the packet's own, which is what makes this route reachable with no
        identity provisioned: the endpoint is already addressed by its session, so an open
        deployment needs no device_id and no key to retune a node.
        """
        prefix = IN_BAND | RF_CONFIG
        payload = bytes([prefix]) + dumps(new_config).encode("utf-8")
        try_for = self._IN_BAND_ATTEMPTS
        while try_for > 0:
            packet = self.create_request(digital_endpoint.get_mac_address(),
                                         digital_endpoint.get_mesh(),
                                         digital_endpoint.get_sleep(),
                                         digital_endpoint.session_id)
            packet.set_kind(Packet_v3.CTRL)
            packet.set_payload(payload)
            if self._is_control_ack(self.send_request(packet), prefix):
                # The peer accepted, so this end follows it onto the new configuration, exactly
                # as the signed route does once its artifact is delivered. Moving only the Edge
                # is not a partial success: it is an endpoint this Hub can no longer hear.
                # Sent between visits, so the next visit already polls on the new config and
                # counts as a probe.
                self._mirror_endpoint_config(digital_endpoint, new_config, mid_visit=False)
                return self.ACCEPTED
            try_for -= 1
        return self._diagnose_in_band_silence(digital_endpoint)

    def _diagnose_in_band_silence(self, digital_endpoint):
        """Tell a peer that declined the command from a peer that is not there at all.

        Both produce the same observation, twenty unanswered rounds, and until now both produced
        the same report. A node holding a control root refuses an unsigned command by answering
        nothing, which is right on the wire: acknowledging would tell this end that a change
        happened which did not. So the silence has to be interrogated rather than read.

        The interrogation is the one request an Edge always replies to, the connection poll. It
        is asked on the very configuration the command went out on, which is sound because this
        end never moves its own radio on the in-band route until an acknowledgement arrives, so
        a failed exchange leaves both ends where they started. Twenty unanswered commands
        followed by an answered poll is not ambiguous: the peer can hear this Hub and chose not
        to acknowledge the command.

        Deliberately the same question session liveness already asks, rather than a second
        liveness concept beside it, and deliberately not gated on posture: an open deployment
        can be commanding a node that was provisioned with a root, and that is exactly the
        half-provisioned rollout this diagnosis is for.
        """
        for _ in range(self._DIAGNOSIS_POLLS):
            poll = self.create_request(digital_endpoint.get_mac_address(),
                                       digital_endpoint.get_mesh(),
                                       digital_endpoint.get_sleep(),
                                       digital_endpoint.session_id)
            answered, _ = self.ask_ok(poll)
            if answered:
                if self.debug:
                    print("Endpoint {} answered a poll after refusing the command: it declined "
                          "rather than went missing (check its signed provisioning)".format(
                              digital_endpoint.session_id))
                return self.REFUSED
        if self.debug:
            print("Endpoint {} answered neither the command nor a poll: report the link, "
                  "not a refusal".format(digital_endpoint.session_id))
        return self.UNREACHABLE

    @staticmethod
    def _is_control_ack(reply, prefix):
        """Whether a reply is *this* command's acknowledgement.

        Matched on the prefix that went out, not merely on the frame being a CTRL: a peer
        answering about some other control type has not accepted this one. An OK is explicitly
        not an ack, because OK is also the connection poll and the keepalive, so treating one
        as acceptance would let a peer that is merely alive read as a peer that retuned. The
        Hub would then move itself onto a config the Edge never applied, which is the deaf
        endpoint the mirror exists to prevent.
        """
        if reply is None or reply.get_command() != Packet_v3.CTRL:
            return False
        payload = reply.get_payload()
        return bool(payload) and payload[0] == prefix

    def _load_control_counter(self):
        """Read back the highest number this Hub has issued, or 0 if it has none.

        Keyed by the root that issued them, exactly as a node keys its own mark, so a Hub given
        a rotated root starts its numbering over. That makes rotating the root the one reset for
        the whole control plane, on both ends, rather than a second mechanism to get right.

        Without a file the count is RAM-only, and a Hub that restarted would re-issue numbers
        its fleet has already accepted: every later artifact would be delivered, verified, and
        then dropped as a replay, with nothing on either side reporting it.
        """
        if not self.control_counter_file or self.control_root is None:
            return 0
        try:
            with open(self.control_counter_file, "r") as f:
                mark = loads(f.read())
            if mark.get("root") != self.control_root.fingerprint().hex():
                return 0    # a different authority: its numbering says nothing about this one
            counter = mark.get("counter", 0)
        except Exception:
            # No file yet, or one we cannot read. Starting from 0 costs nothing on a fresh Hub
            # and is self-correcting on an existing fleet only by rotating the root, which is
            # the same recovery a lost file has.
            counter = 0
        return counter if isinstance(counter, int) and counter > 0 else 0

    def _save_control_counter(self):
        if not self.control_counter_file or self.control_root is None:
            return
        try:
            # Committed through a rename like the config files, and for a sharper reason: a
            # truncated mark reads back as no counter at all, which starts the sequence over,
            # and a fleet only takes numbers above the highest it has already accepted.
            self._commit_json(self.control_counter_file,
                              {"root": self.control_root.fingerprint().hex(),
                               "counter": self._control_counter})
        except Exception as e:
            print("Hub: could not persist the control counter ({})".format(e))

    def _next_control_counter(self):
        # Issue the next number in this Hub's fleet-wide sequence. Never derived from a clock:
        # this may run on a board whose RTC does not survive a power cycle, and a clock that
        # once guessed high would push the number beyond anything a later command could reach,
        # locking every node out of its own control plane until the root is rotated.
        self._control_counter += 1
        # Recorded BEFORE the number is handed out. Losing power after minting but before the
        # write would re-issue a number the target has already accepted, and that artifact is
        # refused as a replay; losing power after the write merely burns one number, and a gap
        # in the sequence costs nothing because a node only requires each artifact to be higher
        # than the last it took.
        self._save_control_counter()
        return self._control_counter

    def endpoint_trial_old(self, digital_endpoint):
        """The last-known-good config the Hub retained for an endpoint in an RF-config trial
        (the probe fallback), or None when no trial is live."""
        trial = self._endpoint_trial.get(digital_endpoint.session_id)
        return trial["old"] if trial else None

    def _mirror_endpoint_config(self, digital_endpoint, mirror, mid_visit=True):
        # Switch the Hub's view of this endpoint to the new config, snapshotting the old as the
        # probe fallback and arming the trial. prepare_connector tunes to endpoint.* on the next
        # visit, so updating those fields IS the Hub following the Edge onto the new config.
        #
        # `mid_visit` says whether the caller is inside a visit that already tuned the radio.
        # A reconfig delivered as a downlink is: the mirror fires when the Edge's final-OK
        # lands, part way through a visit polling on the old config. A command sent on the link
        # itself is not: it happens between visits, so the very next visit polls on the new
        # config and is a real probe. Assuming otherwise throws that probe away, and it is the
        # one the proving exchange arrives in.
        sid = digital_endpoint.session_id
        # The rollback target has to be a real config, never an endpoint's "follow this node"
        # placeholder: giving up on a trial has to put the radio somewhere concrete.
        self.resolve_endpoint_rf(digital_endpoint)
        old = [digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw,
               digital_endpoint.cr, digital_endpoint.tx_power]
        for attr in ("freq", "sf", "bw", "cr", "tx_power"):
            value = mirror.get(attr)
            if value is not None:
                setattr(digital_endpoint, attr, value)
        new = [digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw,
               digital_endpoint.cr, digital_endpoint.tx_power]
        # "fresh" skips the probe advance for the visit the mirror happened in: the Hub polled
        # that whole visit on the OLD config (prepare_connector already ran), so it never
        # actually probed the new one — the first real probe visit is the next one.
        self._endpoint_trial[sid] = {"old": old, "new": new, "misses": 0, "total": 0,
                                     "fresh": mid_visit}
        if self.debug:
            print("Mirrored endpoint {} to new config; old retained {}".format(sid, old))

    def _endpoint_rf3(self, digital_endpoint):
        return [digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw]

    def _set_endpoint_rf(self, digital_endpoint, cfg):
        (digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw,
         digital_endpoint.cr, digital_endpoint.tx_power) = cfg

    def _persist_endpoint_rf(self, digital_endpoint):
        """Write an endpoint's settled RF back to the roster file this Hub registered it from.

        A node writes back the config file it read: the Edge its own LoRa.json, this node the
        Nodes.json it was given. Without this half, a Hub that commanded a retune, saw it
        accepted and was then restarted came back polling the old config while its Edge sat on
        the new one, and neither side could return.

        The roster is overlaid, never rebuilt from the live endpoints: an endpoint keeps only
        the ten-odd keys it models, so rebuilding would drop every key it does not know about
        and delete outright every entry marked inactive, which never becomes an endpoint at
        all. Same defect `backup_config` records one level down.
        """
        # A roster assembled in code has no file behind it, so there is nothing to write back
        # to; naming one is how such a Hub opts in.
        if not self.nodes_file:
            return
        label = digital_endpoint.get_label()
        if label == NO_ADDRESS:
            # Registered with neither a MAC nor a device_id, so it has no durable name to key a
            # record by. It also cannot be polled, so this is a misconfiguration to report
            # rather than a case to engineer around.
            print("Hub: endpoint {} has no address; its config is not persisted".format(
                digital_endpoint.get_name()))
            return
        try:
            with open(self.nodes_file, "r") as f:
                roster = loads(f.read())
            entry = None
            for node in roster:
                if label_for_config(node) == label:
                    entry = node
                    break
            if entry is None:
                # The endpoint outlived its record: registered by MAC and later re-registered
                # by fingerprint, or simply removed from the file. Falling back to what the
                # file says is the safe direction, and it is visible where the operator looks.
                print("Hub: no entry for endpoint {} in {}; config not persisted".format(
                    label, self.nodes_file))
                return
            if not self._overlay_endpoint_rf(entry, digital_endpoint):
                return
            self._commit_json(self.nodes_file, roster)
            if self.debug:
                print("Persisted endpoint {} config: {}".format(
                    label, digital_endpoint.describe_rf()))
        except Exception as e:
            # A roster that cannot be read or written leaves the Hub running on its in-memory
            # view, which is still correct until the next restart. Losing the poll loop over a
            # bookkeeping write would be the worse trade.
            print("Hub: could not persist endpoint {} config ({})".format(label, e))

    def _overlay_endpoint_rf(self, entry, digital_endpoint):
        # Bring one roster entry into line with the config its endpoint settled on, and say
        # whether anything actually changed. Compared against the entry RESOLVED, not against
        # its raw keys: an entry that states no RF means "poll me wherever you already are",
        # so an endpoint that rolled back to exactly that is unchanged and must not be pinned
        # by a block it never asked for.
        stated = Digital_Endpoint(entry)
        stated.resolve_rf(self.rf_defaults)
        if all(getattr(stated, field) == getattr(digital_endpoint, field)
               for field in Digital_Endpoint._RF_FIELDS):
            return False
        block = entry.get("connector", {})
        for field, key in Digital_Endpoint._RF_FROM_CONNECTOR.items():
            block[key] = getattr(digital_endpoint, field)
        entry["connector"] = block
        # The legacy flat spelling is superseded by the block, which wins outright when both
        # are present. Leaving it would give the operator a file that contradicts itself.
        for field in Digital_Endpoint._RF_FIELDS:
            if field in entry:
                del entry[field]
        return True

    def _probe_visit_end(self, digital_endpoint, heard, completed):
        # Advance the {new, old} probe once per visit. The endpoint's current config IS the
        # config this visit polled on (prepare_connector tuned to it); the trial retains both
        # candidates so the next visit can swap.
        sid = digital_endpoint.session_id
        trial = self._endpoint_trial.get(sid)
        if trial is None:
            return
        if trial.get("fresh"):
            # The mirror visit polled entirely on the old config; the real probe starts next visit.
            trial["fresh"] = False
            return
        active_is_old = self._endpoint_rf3(digital_endpoint) == trial["old"][:3]

        if completed or (heard and active_is_old):
            # Confirmed. Either a full-payload exchange proved the NEW config works (commit), or
            # the Edge was re-acquired on the OLD config (it self-restored; the new config
            # failed). Both settle on the config now in force — drop the trial, stop probing.
            if self.debug:
                which = "old (Edge rolled back)" if active_is_old else "new (committed)"
                print("RF probe settled endpoint {} on {}".format(sid, which))
            self._endpoint_trial.pop(sid, None)
            # The config stopped being provisional, so now it can be written down. Never
            # earlier: a Hub that persisted the optimistic value and then restarted mid-trial
            # would come back on the new config while the Edge, starved of the exchange its
            # trial needed, restored the old one. That split does not heal, while persisting
            # nothing leaves both ends converging on old unaided.
            self._persist_endpoint_rf(digital_endpoint)
            self._settle_rf_change(sid, self.REFUSED if active_is_old else self.ACCEPTED)
            return

        if heard:
            # Located on the NEW config but no full exchange yet: hold here, keep probing for one
            # (a short frame is not proof a max-payload chunk will land — the v2 false-positive).
            trial["misses"] = 0
            return

        # Silence this visit. Count it; after the per-config budget, swap {new, old} to look for
        # a rolled-back Edge; after the total budget, give up and restore to old (best re-contact).
        trial["misses"] += 1
        trial["total"] += 1
        if trial["total"] >= self.probe_give_up_after:
            if self.debug:
                print("RF probe gave up on endpoint {}; restoring old config".format(sid))
            self._set_endpoint_rf(digital_endpoint, trial["old"])
            self._endpoint_trial.pop(sid, None)
            # Restored to where the file already had it, so this normally writes nothing; it
            # runs anyway because "the trial is over" is the one rule, and an endpoint that
            # was pinned by an earlier change is still owed a correction.
            self._persist_endpoint_rf(digital_endpoint)
            # Located on neither configuration, so nothing here says the peer refused anything.
            # Same rule the in-band route follows: an outcome nobody answered for is reported
            # as the link, never as a decision the peer did not make.
            self._settle_rf_change(sid, self.UNREACHABLE)
            return
        if trial["misses"] >= self.probe_swap_after:
            trial["misses"] = 0
            target = trial["new"] if active_is_old else trial["old"]
            self._set_endpoint_rf(digital_endpoint, target)
            if self.debug:
                print("RF probe swapped endpoint {} to {}".format(sid, target[:3]))

    def _session_visit_end(self, digital_endpoint):
        # Decide whether the session held for this endpoint is still usable, and tear it down
        # if it is not, so the next visit handshakes from scratch. That puts a rebooted
        # endpoint back in exactly the state of one never contacted, which the drive loop
        # already knows how to bootstrap, instead of leaving the pair stranded on half a
        # session until this node restarts.
        #
        # Silence on its own is NOT evidence of a dead session. An Edge with no file answers
        # nothing at all: it stays quiet through a metadata poll rather than spend airtime
        # saying "nothing yet", so an idle sensor and a rebooted one look identical from here.
        # Treating silence as proof would re-key every idle endpoint every few visits, and an
        # ECDH is the most expensive thing either end ever does.
        #
        # So a run of silence only raises the question, and the answer comes from the one
        # request an Edge always replies to: the connection poll. Re-arm it, and let the next
        # visit ask. Answered means the endpoint was merely idle. Silent again means it is not
        # answering anything it can hear, which is the signal worth spending a session on.
        if self.security_mode != 'secure' or self.session_store is None:
            return
        sid = digital_endpoint.session_id
        if self._peer_alive_this_visit:
            self._session_silent_visits.pop(sid, None)
            return
        if self.session_store.get(sid) is None:
            return          # nothing held, so nothing to tear down
        silent, polled = self._session_silent_visits.get(sid, (0, False))
        silent += 1
        if polled:
            # The connection poll went unanswered too: not idle, out of step.
            self._session_silent_visits.pop(sid, None)
            self.session_store.drop(sid)
            # Every delegation that failed while the session was dead failed for that one
            # reason, so the skip they earned is now measuring a condition that no longer
            # exists. Left in place it outlives the repair and keeps deferring the very
            # downlink the repair was for, which on this direction can be a queued command.
            self._delegation_backoff.pop(sid, None)
            if self.debug:
                print("Endpoint {} did not answer a connection poll after {} silent visits; "
                      "dropping the session so the next visit re-handshakes".format(sid, silent))
            return
        if silent < self.session_recovery_after:
            self._session_silent_visits[sid] = (silent, False)
            return
        # Budget reached: ask the question rather than assume the answer.
        self._session_silent_visits[sid] = (silent, True)
        self._rearm_connection_poll(digital_endpoint)
        if self.debug:
            print("Endpoint {} silent for {} visits; re-arming the connection poll to tell "
                  "idle from out of step".format(sid, silent))

    @staticmethod
    def _rearm_connection_poll(digital_endpoint):
        # Send the endpoint back to its pre-contact state so the next visit opens with an OK
        # poll. A reassembly still in flight is already dead if the peer has gone this quiet;
        # dropping it releases its writer and temp file (set_current_file owns that now).
        digital_endpoint.set_current_file(None)
        digital_endpoint.state = Digital_Endpoint.OK

    def downlink_pending(self, digital_endpoint):
        source = self._downlink.get(digital_endpoint.session_id)
        return source is not None and source.has_pending()

    def _maybe_delegate(self, digital_endpoint):
        sid = digital_endpoint.session_id
        source = self._downlink.get(sid)
        if source is None:
            return False
        # Pump a live feed before deciding: a broker message that just arrived can be
        # delegated this very boundary. No-op for the default queue; never blocks.
        source.check()
        if not source.has_pending():
            return False
        backoff = self._delegation_backoff.get(sid)
        if backoff and backoff[1] > 0:
            # Recent delegations died unanswered: skip this boundary (exponentially
            # more of them each failure) and let the round poll the uplink instead.
            # The file stays queued for a later, healthier boundary.
            backoff[1] -= 1
            return False
        file = source.peek_file()
        self._swap_id = (self._swap_id + 1) & 0xFF
        grant = self.new_packet()
        grant.set_session(digital_endpoint.session_id)
        grant.set_grant(self._swap_id)
        # Fire-and-forget, like the final-OK: a lost GRANT is benign (the Edge never left
        # its source role). It just costs this reclaim window before the poll resumes.
        self.send_lora(grant)
        if self.debug:
            print("GRANT({}) to sid {}".format(self._swap_id, digital_endpoint.session_id))

        self.current_role = "source"
        self._delegated_sid = digital_endpoint.session_id
        self.set_file(file)
        try:
            delivered = self._serve_until_reclaimed(self.reclaim_timeout)
        finally:
            self.file = None
            self._delegated_sid = None
            self.current_role = "collector"
        if delivered:
            source.confirm_file()
            self._delegation_backoff.pop(sid, None)
            # A pull that ran to completion is the strongest liveness evidence there is, and a
            # visit whose only exchange was that pull would otherwise look silent.
            self._peer_alive_this_visit = True
            # The Edge acknowledged the reconfig downlink (final-OK heard): it will apply the
            # new config after its pull's final-OK, so the Hub mirrors now and starts probing.
            mirror = self._pending_mirror.pop(sid, None)
            if mirror is not None:
                self._mirror_endpoint_config(digital_endpoint, mirror)
        else:
            failures = (backoff[0] if backoff else 0) + 1
            # Cap the skip so a healed Edge is retried within a bounded number of
            # boundaries; the cost of one futile delegation is one reclaim window.
            self._delegation_backoff[sid] = [failures, min(2 ** failures, 32)]
        return True

    def _serve_until_reclaimed(self, reclaim_s):
        # Reclaim is event-driven with the timer as backstop: the Edge's final-OK ends this
        # loop at once (the file completes), while the inactivity deadline only fires when
        # the Edge never drives (lost GRANT) or stops driving mid-pull. Then the poll
        # resumes, and any late-driving Edge yields to it on hearing the next poll.
        deadline = ticks_add(time(), reclaim_s * 1000)
        while not self.file.sent and ticks_diff(deadline, time()) > 0:
            packet = self.respond(self._respond_handler)
            if packet is not None and self.is_for_me(packet):
                deadline = ticks_add(time(), reclaim_s * 1000)
            gc.collect()
        return self.file.sent

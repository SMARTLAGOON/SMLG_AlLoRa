"""Hub: the center-placement authority (formerly `Requester`/`Collector`).

Named by where it sits: the Hub is the permanent controller of its Edges. It polls, pulls
their uplink files, and never surrenders control. Its home role is "collector" (the drive
loop); it also carries the serve loop because a downlink temporarily reverses the roles:
the Hub delegates the drive role to an Edge with a GRANT, serves the pending file to the
Edge's pull, and reclaims control the moment the pull ends (or its reclaim timer fires).
`Gateway` remains the multi-endpoint preset built on top of this.
"""
import gc
from os import urandom
from AlLoRa.Nodes.Swap_base import Swap_base
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.utils.time_utils import current_time_ms as time, ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print


class _Downlink_queue(DataSource):
    """The Hub's default per-Edge downlink source: a plain FIFO behind the DataSource
    seam. Unlike the base queue it neither dedupes by name nor evicts on overflow: a
    command queued twice must be delivered twice, and dropping a queued control file
    to make room would be silent data loss on the catastrophic direction."""

    def add_to_queue(self, file):
        self.file_queue.append(file)


class Hub(Swap_base):

    def __init__(self, connector=None, config_file="LoRa.json",
                 debug_hops=False,
                 max_sleep_time=3,
                 successful_interactions_required=5,
                 data_sink=None,
                 reclaim_timeout=10,
                 probe_swap_after=1,
                 probe_give_up_after=6,
                 session_recovery_after=3):
        super().__init__(connector, config_file,
                         debug_hops=debug_hops,
                         max_sleep_time=max_sleep_time,
                         successful_interactions_required=successful_interactions_required,
                         data_sink=data_sink,
                         home_role="collector")
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
        # Secure-session liveness, counted in visits. A peer that reboots loses its RAM-held
        # session and has no way to say so, and only this side can offer a new one, so the
        # authority has to notice by itself. How that is decided (and why silence alone does
        # not decide it) is in _session_visit_end. Per sid: (consecutive silent visits,
        # whether the confirming connection poll is already armed).
        self.session_recovery_after = session_recovery_after
        self._session_silent_visits = {}

    def set_downlink_source(self, digital_endpoint, datasource):
        """Plug a live input boundary (e.g. an MQTT_Datasource) as this Edge's downlink:
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

    def endpoint_trial_old(self, digital_endpoint):
        """The last-known-good config the Hub retained for an endpoint in an RF-config trial
        (the probe fallback), or None when no trial is live."""
        trial = self._endpoint_trial.get(digital_endpoint.session_id)
        return trial["old"] if trial else None

    def _mirror_endpoint_config(self, digital_endpoint, mirror):
        # Switch the Hub's view of this endpoint to the new config, snapshotting the old as the
        # probe fallback and arming the trial. prepare_connector tunes to endpoint.* on the next
        # visit, so updating those fields IS the Hub following the Edge onto the new config.
        sid = digital_endpoint.session_id
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
                                     "fresh": True}
        if self.debug:
            print("Mirrored endpoint {} to new config; old retained {}".format(sid, old))

    def _endpoint_rf3(self, digital_endpoint):
        return [digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw]

    def _set_endpoint_rf(self, digital_endpoint, cfg):
        (digital_endpoint.freq, digital_endpoint.sf, digital_endpoint.bw,
         digital_endpoint.cr, digital_endpoint.tx_power) = cfg

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
        # poll. A reassembly still in flight is already dead if the peer has gone this quiet,
        # and its buffer holds an open file handle nothing else will close, so release it
        # here rather than leak it (the on-device descriptor table is tiny).
        in_flight = digital_endpoint.get_current_file()
        if in_flight is not None:
            in_flight.discard()
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
        # its serve role). It just costs this reclaim window before the poll resumes.
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

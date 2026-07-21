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
                 reclaim_timeout=10):
        super().__init__(connector, config_file,
                         debug_hops=debug_hops,
                         max_sleep_time=max_sleep_time,
                         successful_interactions_required=successful_interactions_required,
                         data_sink=data_sink,
                         home_role="collector")
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

    def set_downlink_source(self, digital_endpoint, datasource):
        """Plug a live input boundary (e.g. an MQTT_Datasource) as this Edge's downlink:
        whatever it queues is delivered by delegation, exactly like queue_downlink files.
        Brought up here, at registration: a connect that must fail should fail at setup,
        loudly, not mid-drive-loop. Replaces the endpoint's previous source, so register
        before queueing anything."""
        datasource.prepare()
        self._downlink[digital_endpoint.session_id] = datasource

    def queue_downlink(self, digital_endpoint, file):
        """Queue an outbound file for one Edge. It is delivered by delegation: at the next
        safe boundary the drive loop sends GRANT and serves this file to the Edge's pull."""
        sid = digital_endpoint.session_id
        source = self._downlink.get(sid)
        if source is None:
            source = _Downlink_queue(self.chunk_size)
            self._downlink[sid] = source
        source.add_to_queue(file)

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

"""Hub — the center-placement authority (formerly `Requester`/`Collector`).

Named by where it sits: the Hub is the permanent controller of its Edges — it polls, pulls
their uplink files, and never surrenders control. Its home role is "collector" (the drive
loop); it also carries the serve loop because a downlink temporarily reverses the roles:
the Hub delegates the drive role to an Edge with a GRANT, serves the pending file to the
Edge's pull, and reclaims control the moment the pull ends (or its reclaim timer fires).
`Gateway` remains the multi-endpoint preset built on top of this.
"""
import gc
from os import urandom
from AlLoRa.Nodes.Swap_base import Swap_base
from AlLoRa.utils.time_utils import current_time_ms as time, ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print


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
        # Downlink outbound, per endpoint sid. A file stays queued until an Edge's completed
        # pull confirms delivery, so a failed delegation simply retries at the next boundary.
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

    def queue_downlink(self, digital_endpoint, file):
        """Queue an outbound file for one Edge. It is delivered by delegation: at the next
        safe boundary the drive loop sends GRANT and serves this file to the Edge's pull."""
        self._downlink.setdefault(digital_endpoint.session_id, []).append(file)

    def downlink_pending(self, digital_endpoint):
        return bool(self._downlink.get(digital_endpoint.session_id))

    def _maybe_delegate(self, digital_endpoint):
        sid = digital_endpoint.session_id
        queue = self._downlink.get(sid)
        if not queue:
            return False
        backoff = self._delegation_backoff.get(sid)
        if backoff and backoff[1] > 0:
            # Recent delegations died unanswered: skip this boundary (exponentially
            # more of them each failure) and let the round poll the uplink instead.
            # The file stays queued for a later, healthier boundary.
            backoff[1] -= 1
            return False
        file = queue[0]
        self._swap_id = (self._swap_id + 1) & 0xFF
        grant = self.new_packet()
        grant.set_session(digital_endpoint.session_id)
        grant.set_grant(self._swap_id)
        # Fire-and-forget, like the final-OK: a lost GRANT is benign — the Edge never left
        # its serve role — it just costs this reclaim window before the poll resumes.
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
            queue.pop(0)
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
        # the Edge never drives (lost GRANT) or stops driving mid-pull — then the poll
        # resumes, and any late-driving Edge yields to it on hearing the next poll.
        deadline = ticks_add(time(), reclaim_s * 1000)
        while not self.file.sent and ticks_diff(deadline, time()) > 0:
            packet = self.respond(self._respond_handler)
            if packet is not None and self.is_for_me(packet):
                deadline = ticks_add(time(), reclaim_s * 1000)
            gc.collect()
        return self.file.sent

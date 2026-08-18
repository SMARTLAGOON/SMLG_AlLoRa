"""Edge: the node at the far placement (formerly `Source`).

Named by where it sits, not by which way data flows: an Edge lives with the sensors/devices
at the end of the link and *serves* by default (home role "source"): uplink is always
Edge-serves / Hub-pulls, even for a large file. It still carries the drive loop, because a
downlink reverses the roles for one pull: the Hub delegates the collector role with a GRANT and
this Edge pulls the pending file, then comes home. The Edge never self-promotes, and it
yields the instant it hears its Hub polling again. `data_sink` is therefore where a
*downlink* lands on an Edge (a capturing sink in tests, an apply-the-artifact sink in
production).
"""
import gc
from AlLoRa.Nodes.Node import Node
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.utils.time_utils import current_time_ms as time, ticks_add, ticks_diff
from AlLoRa.utils.debug_utils import print


class Edge(Node):

    def __init__(self, connector=None, config_file="LoRa.json", data_sink=None,
                 datasource=None, control_actuator=None, downlink_window=None,
                 downlink_stall_timeout=None):
        super().__init__(connector, config_file, data_sink=data_sink,
                         datasource=datasource, control_actuator=control_actuator,
                         home_role="source")
        # Two limits on a granted pull, and only the second one should ever end a healthy
        # transfer. Both read from LoRa.json like every other tunable, with the constructor
        # argument as the override, because a deployment configures a node through its file.
        #
        # downlink_window is the ceiling on the whole pull. It exists so a pull always ends,
        # and it is deliberately generous: sizing it against the artifact is what made a 1 MiB
        # downlink impossible to complete without knowing its transfer time in advance.
        #
        # downlink_stall_timeout is the one that does the work. It ends a pull that has stopped
        # advancing, so an Edge whose Hub died still comes home and serves its own data again.
        self.downlink_window = downlink_window if downlink_window is not None \
            else self.config.get('downlink_window', 7200)
        self.downlink_stall_timeout = downlink_stall_timeout if downlink_stall_timeout is not None \
            else self.config.get('downlink_stall_timeout', 60)
        self._grant_pending = None
        self._last_swap_id = None

    def _on_grant(self, packet):
        swap_id = packet.get_swap_id()
        if swap_id is None or swap_id == self._last_swap_id:
            return    # malformed, or a duplicate of a delegation already completed
        self._grant_pending = swap_id

    def run(self, timeout=None):
        """The Edge's main loop: answer the Hub's polls and uplink pulls (source role), and
        honor a GRANT by temporarily driving one downlink pull, then come home. `timeout`
        is in seconds; None runs forever (the deployed main loop).

        Both node types run with the same verb: `Edge(...).run()` and `Hub(...).run()`. It is
        deliberately not called `serve` here, even though serving is what an Edge mostly does,
        because `serve` names one of the two things a node does in a round, and an Edge that
        honors a GRANT spends part of this very loop driving instead."""
        end_time = None if timeout is None else ticks_add(time(), timeout * 1000)
        while end_time is None or ticks_diff(end_time, time()) > 0:
            self._pump_datasource()
            self.respond(self._respond_handler)
            if self.file is not None and self.file.sent:
                # The uplink completed (the Hub's final-OK landed): retire it, or the idle
                # metadata poll would serve the same file over and over. That final-OK is
                # also the confirmation a queued file needs before it leaves the queue, so
                # this is the one place an uplink may drop it.
                self._retire_file(True)
            self._service_grant()
            self._service_trial_window()
            gc.collect()

    def serve(self, timeout=None):
        """Deprecated name for `run()`, kept so existing Edge main loops call it unchanged."""
        return self.run(timeout=timeout)

    def _service_grant(self):
        if self._grant_pending is not None:
            swap_id = self._grant_pending
            self._grant_pending = None
            if self._pull_downlink():
                # Only a COMPLETED pull retires the delegation id. A failed pull must
                # leave it honorable: the retry may legitimately carry the same id
                # (a rebooted Hub restarts its counter), and going deaf to it costs
                # a whole reclaim window with no log.
                self._last_swap_id = swap_id
                # The pull is done and its final-OK is on the air, so this is the safe
                # boundary to apply any control action a downlink control sink deferred
                # during consume() (an RF-config switch or a reset). Acting earlier would
                # switch the radio (or reboot) before the Hub was acknowledged.
                self._run_pending_control()

    def _pull_downlink(self):
        endpoint = self._capture_hub_endpoint()
        if self.debug:
            print("GRANT honored: pulling downlink from sid {}".format(endpoint.session_id))
        self.current_role = "collector"
        delivered = False
        try:
            delivered = self.listen_to_endpoint(endpoint, listening_time=self.downlink_window,
                                                save_file=True, one_file=True,
                                                stall_timeout=self.downlink_stall_timeout)
            return delivered
        finally:
            self.current_role = "source"
            if not delivered:
                # A dead pull's half-built reassembly buffer holds an open file
                # handle, and the endpoint dies with this method: nobody else can
                # release it. A later retry starts a fresh buffer regardless, so
                # close and drop this one (the FD table on-device is tiny).
                in_flight = endpoint.get_current_file()
                if in_flight is not None:
                    in_flight.discard()

    def _capture_hub_endpoint(self):
        # The Hub endpoint is captured from the live session, never configured: by the time
        # a GRANT can arrive the session already exists, so the Edge addresses the Hub by
        # the sid both ends share, drives on the RF config already in use, and adds only a
        # fresh reassembly buffer (the endpoint starts with none). It skips the OK poll:
        # the GRANT itself means the downlink is ready.
        c = self.connector
        endpoint = Digital_Endpoint(config={
            "name": "hub", "mac_address": "hub", "active": True,
            "freq": c.frequency, "sf": c.sf, "bw": c.bw, "cr": c.cr,
            "tx_power": c.tx_power, "session_id": self.session_id,
        })
        endpoint.state = Digital_Endpoint.REQUEST_DATA_STATE
        return endpoint

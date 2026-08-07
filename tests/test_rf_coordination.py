"""Acceptance — v3 two-ended RF_CONFIG coordination (the "Hub-mirror" reconvergence).

A signed downlink RF_CONFIG must make a radio-parameter change stick on BOTH ends of the
Edge<->Hub link — over the very link the change might break. The Edge-side actuator defers
the switch until after the downlink's final-OK; this layer makes the change reconverge:

  * The Edge (weak end) self-restores to last-known-good if the new config proves unreachable
    (a silent or stalled trial window) — see tests/test_serve_engine.py for that engine.
  * The Hub (authority) MIRRORS the endpoint config after it hears the reconfig downlink's
    final-OK (queue_downlink(..., mirror_config=...)), then PROBES the {new, old} pair on its
    normal poll loop to re-acquire the Edge wherever it actually landed, committing the new
    config only on a full-payload exchange and restoring to old on total failure.

The link double models the physics the coordination must survive: two ends on different
(freq, sf, bw) cannot demodulate each other, and a genuinely bad config is a dead link.
"""
import json
import queue
import threading
import time

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.Control.control_types import RF_CONFIG
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.DataSinks.DataSink import DataSink, Reception
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.File import AlLoRa_File
from test_control_root_sink import (
    CONTROL_ROOT_PRIV, TARGET_DEVICE_ID, _CapturingActuator, _artifact,
)

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
SESSION_ID = 42
HUB_OWN_SID = 7


class Rf_gated_loopback(Loopback_connector):
    """A loopback that models the RF physics the coordination must survive.

    A transmitted frame carries the sender's (freq, sf, bw) at transmit time; the receiver
    drops it unless its OWN current (freq, sf, bw) matches — two ends on different configs
    cannot demodulate each other. Extra knobs:
      * break_outbound(): this end's transmits vanish (a one-directional dead link, Q5).
      * set_dead_rf(sf=..): frames sent while on a matching (freq, sf, bw) vanish even when
        both ends match it — a config that simply cannot close the link (Q1 "unreachable").
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._broken_out = False
        self._dead = set()      # partial (freq, sf, bw) specs; None fields are wildcards
        self.gated = 0          # frames dropped by the RF gate (mismatch / dead / broken)

    def break_outbound(self, broken=True):
        self._broken_out = broken

    def set_dead_rf(self, freq=None, sf=None, bw=None):
        self._dead.add((freq, sf, bw))

    def _rf3(self):
        freq, sf, bw, _, _ = self.get_rf_config()
        return (freq, sf, bw)

    def _is_dead(self, rf3):
        freq, sf, bw = rf3
        for df, ds, db in self._dead:
            if df in (None, freq) and ds in (None, sf) and db in (None, bw):
                return True
        return False

    def transmit(self, wire):
        rf3 = self._rf3()
        if self._broken_out or self._is_dead(rf3):
            self.gated += 1
            return True         # "sent" fine; the channel ate it
        self.outbox.put((rf3, wire))
        return True

    def recv(self, focus_time=12):
        deadline = time.monotonic() + focus_time
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                sender_rf3, wire = self.inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            if sender_rf3 == self._rf3():
                return wire
            self.gated += 1     # heard noise on the wrong config; keep listening this window


def _make_gated_pair():
    a_to_b, b_to_a = queue.Queue(), queue.Queue()
    edge_conn = Rf_gated_loopback(EDGE_MAC, inbox=b_to_a, outbox=a_to_b)
    hub_conn = Rf_gated_loopback(HUB_MAC, inbox=a_to_b, outbox=b_to_a)
    return edge_conn, hub_conn


class Capture_sink(DataSink):
    def __init__(self):
        self.received = []

    def consume(self, file, reception=None):
        self.received.append((file.get_name(), bytes(file.get_content()), reception))
        file.discard()


class Rf_apply_sink(Capture_sink):
    """The Edge's downlink sink for these tests: it treats a delivered file as an RF_CONFIG
    artifact (its content is the JSON payload) and defers the switch exactly as the real
    Node_Control_Actuator does — queue the change, let the Edge drain it after the pull's final-OK.
    Keeps the coordination test off the crypto gate (verified end-to-end in test_node_control_sink).
    """

    def __init__(self, node):
        super().__init__()
        self.node = node

    def consume(self, file, reception=None):
        cfg = json.loads(bytes(file.get_content()).decode("utf-8"))
        change_rf_config = self.node.change_rf_config
        self.node.queue_control_action(lambda: change_rf_config(cfg))
        super().consume(file, reception)


def _write_config(path, result_path, session_id):
    config = {
        "name": "rf", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": session_id,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_pair(tmp_path, edge_conn, hub_conn, edge_sink=None, **hub_kwargs):
    edge_config = str(tmp_path / "edge.json")
    hub_config = str(tmp_path / "hub.json")
    _write_config(edge_config, str(tmp_path / "edge_results"), SESSION_ID)
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)

    edge = Edge(edge_conn, config_file=edge_config)
    sink = edge_sink if edge_sink is not None else Rf_apply_sink(edge)
    edge.data_sink = sink
    hub = Hub(hub_conn, config_file=hub_config, **hub_kwargs)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC,
                                active=True, session_id=SESSION_ID)
    return edge, hub, endpoint, sink


def _rf_config_file(cfg, hub):
    return AlLoRa_File(name="rf.json",
                       content=bytearray(json.dumps(cfg).encode("utf-8")),
                       chunk_size=hub.get_chunk_size())


class _Uplink_source(DataSource):
    """A datasource pre-loaded with one uplink file: the Edge serves it once it is idle again
    (after the reconfig), which is what lets the Hub re-acquire the Edge on whichever config
    actually carries it."""

    def __init__(self, chunk_size, name, payload):
        super().__init__(chunk_size)
        self.add_to_queue(AlLoRa_File(name=name, content=bytearray(payload),
                                      chunk_size=chunk_size))


def _run_rotation(hub, endpoint, visits, listening_time):
    # Model the Gateway's round-robin: repeated per-endpoint visits (one probe per visit),
    # stopping early once the RF-config trial has settled.
    for _ in range(visits):
        hub.listen_to_endpoint(endpoint, listening_time=listening_time, save_file=True)
        if hub.endpoint_trial_old(endpoint) is None and not hub.downlink_pending(endpoint):
            break


# --- Slice H3: end-to-end reconvergence on OLD when the new config is a dead link ----------

def test_reconverges_on_old_when_the_new_config_is_unreachable(tmp_path):
    # The signed RF_CONFIG switches both ends to sf9 — but sf9 is a dead link here. The Edge
    # applies it, hears silence, and self-restores to sf7 (last-known-good). The Hub mirrors to
    # sf9, probes {sf9, sf7}, finds the rolled-back Edge on sf7, and settles the trial there.
    # Both ends reconverge on the old config with no manual intervention.
    edge_conn, hub_conn = _make_gated_pair()
    edge_conn.set_dead_rf(sf=9)
    hub_conn.set_dead_rf(sf=9)

    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=2, probe_swap_after=1,
                                           probe_give_up_after=12)
    edge.datasource = _Uplink_source(edge.get_chunk_size(), "up.bin",
                                     bytes((i * 3) % 256 for i in range(300)))

    hub.queue_downlink(endpoint, _rf_config_file({"sf": 9, "trial": 1}, hub),
                       mirror_config={"sf": 9, "trial": 1})

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 30},
                              name="edge-serve", daemon=True)
    server.start()
    _run_rotation(hub, endpoint, visits=25, listening_time=1.0)
    server.join(timeout=32)
    assert not server.is_alive(), "the Edge serve loop never came home"

    # The reconfig was delivered, then both ends fell back to the last-known-good sf7.
    assert ("rf.json", b'{"sf": 9, "trial": 1}') in [(n, c) for n, c, _ in sink.received]
    assert hub.endpoint_trial_old(endpoint) is None, "the Hub trial never settled"
    assert endpoint.sf == 7, "the Hub did not reconverge the endpoint on the old config"
    assert edge.connector.get_rf_config()[1] == 7, "the Edge did not self-restore to sf7"
    assert not edge.sf_trial, "the Edge trial is resolved"


# --- Slice H4: the asymmetric case (Q5) — new reachable Hub->Edge, broken Edge->Hub --------

def test_reconverges_when_the_new_config_uplink_is_one_way_broken(tmp_path):
    # The marginal-link asymmetry Q4/Q5 warn about: on sf9 the Hub reaches the Edge, but the
    # Edge's larger replies never make it back (edge->hub dead on sf9 only). The Edge never sees
    # its uplink land, so it self-restores to sf7; the Hub, hearing nothing on sf9, swaps its
    # probe to sf7 and re-acquires the Edge there. A full uplink then completes on sf7.
    edge_conn, hub_conn = _make_gated_pair()
    edge_conn.set_dead_rf(sf=9)      # Edge->Hub broken on the new config only; Hub->Edge fine

    hub_sink = Capture_sink()
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=2, data_sink=hub_sink,
                                           probe_swap_after=1, probe_give_up_after=20)
    up = bytes((i * 5) % 256 for i in range(300))
    edge.datasource = _Uplink_source(edge.get_chunk_size(), "up.bin", up)

    hub.queue_downlink(endpoint, _rf_config_file({"sf": 9, "trial": 1}, hub),
                       mirror_config={"sf": 9, "trial": 1})

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 40},
                              name="edge-serve", daemon=True)
    server.start()
    _run_rotation(hub, endpoint, visits=35, listening_time=1.5)
    server.join(timeout=42)
    assert not server.is_alive(), "the Edge serve loop never came home"

    assert hub.endpoint_trial_old(endpoint) is None, "the Hub trial never settled"
    assert endpoint.sf == 7, "the Hub did not reconverge the endpoint on the old config"
    assert edge.connector.get_rf_config()[1] == 7, "the Edge did not self-restore to sf7"
    # And once both are back on the working config, the uplink actually gets through.
    assert ("up.bin", up) in [(n, c) for n, c, _ in hub_sink.received], \
        "the uplink never completed after reconvergence"


# --- Slice H5: the happy path — the new config works, so it commits on both ends -----------

def test_new_config_commits_on_both_ends_when_it_works(tmp_path):
    # The reconfig to sf9 is reachable. The Edge applies it and a full uplink completes on sf9,
    # so the Edge commits (no rollback). The Hub mirrors to sf9, re-tunes, pulls that full
    # exchange, and commits the trial on the new config. Both ends stick on sf9.
    edge_conn, hub_conn = _make_gated_pair()      # no dead configs: sf9 is a good link

    hub_sink = Capture_sink()
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=2, data_sink=hub_sink,
                                           probe_swap_after=2, probe_give_up_after=20)
    up = bytes((i * 9) % 256 for i in range(400))
    edge.datasource = _Uplink_source(edge.get_chunk_size(), "up.bin", up)

    # A long trial window: the working config must NOT self-restore before it commits.
    hub.queue_downlink(endpoint, _rf_config_file({"sf": 9, "trial": 30}, hub),
                       mirror_config={"sf": 9, "trial": 30})

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 30},
                              name="edge-serve", daemon=True)
    server.start()
    _run_rotation(hub, endpoint, visits=20, listening_time=1.5)
    server.join(timeout=32)
    assert not server.is_alive(), "the Edge serve loop never came home"

    assert hub.endpoint_trial_old(endpoint) is None, "the Hub trial never committed"
    assert endpoint.sf == 9, "the committed endpoint must stay on the new config"
    assert edge.connector.get_rf_config()[1] == 9, "the Edge rolled back a config that worked"
    assert not edge.sf_trial, "the Edge trial committed"
    assert ("up.bin", up) in [(n, c) for n, c, _ in hub_sink.received], \
        "the uplink that proved the new config never reached the Hub"


# --- Slice H1: the Hub mirrors the endpoint config after the reconfig downlink lands -------

def test_hub_mirrors_endpoint_config_after_the_reconfig_downlink_is_delivered(tmp_path):
    # queue_downlink carries the backend-minted mirror_config (the Hub never parses the signed
    # envelope). After the Edge's final-OK for that downlink (confirm_file), the Hub switches
    # its VIEW of the endpoint to the new config and snapshots the old, entering a trial. The
    # plain loopback here ignores RF, so the point under test is only the mirror bookkeeping.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn, reclaim_timeout=3)

    # This endpoint states no RF of its own, so it follows the Hub, and the Hub is on sf7.
    hub.resolve_endpoint_rf(endpoint)
    assert endpoint.sf == 7, "the endpoint starts on the old config"
    hub.queue_downlink(endpoint, _rf_config_file({"sf": 9, "trial": 30}, hub),
                       mirror_config={"sf": 9, "trial": 30})

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    # The downlink was delivered, and the Hub mirrored the endpoint to sf9 with sf7 retained.
    assert [(n, c) for n, c, _ in sink.received] == [("rf.json", b'{"sf": 9, "trial": 30}')]
    assert endpoint.sf == 9, "the Hub did not mirror the endpoint to the new config"
    assert hub.endpoint_trial_old(endpoint) == [868, 7, 125, 1, 14], \
        "the Hub must retain the old config as the probe fallback"
    assert not hub.downlink_pending(endpoint)


def test_ask_change_rf_moves_both_ends_in_one_call(tmp_path):
    # D4's whole claim. The Hub mints the artifact, queues it, and attaches the mirror itself,
    # so an operator cannot reconfigure the Edge and forget to follow it onto the new config.
    # The artifact is also fed back through a real gate afterwards: chunking and reassembly must
    # not disturb a signature, or the whole signed path fails only over a radio.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    root = Control_Root(CONTROL_ROOT_PRIV)
    sink = Capture_sink()
    edge, hub, _, _ = _make_pair(tmp_path, edge_conn, hub_conn, edge_sink=sink,
                                 reclaim_timeout=3, control_root=root,
                                 control_counter_file=str(tmp_path / "control.counter"))
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                session_id=SESSION_ID, device_id=TARGET_DEVICE_ID.hex())

    hub.resolve_endpoint_rf(endpoint)
    assert endpoint.sf == 7, "the endpoint starts on the old config"

    hub.ask_change_rf(endpoint, {"sf": 9, "trial": 30})

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    assert [name for name, _, _ in sink.received] == ["ctrl.bin"], \
        "the minted artifact must reach the Edge"
    assert endpoint.sf == 9, "one call must also move the Hub onto the new config"
    assert hub.endpoint_trial_old(endpoint) == [868, 7, 125, 1, 14], \
        "the Hub must retain the old config as the probe fallback"

    actuator = _CapturingActuator()
    gate = Control_Root_DataSink(control_root=root.public_key(), device_id=TARGET_DEVICE_ID,
                                 actuator=actuator, counter_file=str(tmp_path / "mark.json"))
    gate.consume(_artifact(tmp_path, sink.received[0][1]), Reception(source="hub"))
    assert actuator.applied == [(RF_CONFIG, b'{"sf": 9, "trial": 30}')], \
        "what arrived over the link must still verify against the root that minted it"


# --- Slice H2: the {new, old} probe state machine (one probe per visit) --------------------

def _armed_hub(tmp_path):
    # A Hub whose endpoint has just been mirrored to the new config (sf9), old sf7 retained —
    # the state the probe drives from. swap_after=1 so a single silent visit swaps.
    hub_config = str(tmp_path / "hub.json")
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)
    hub_conn, _ = Loopback_connector.create_pair(HUB_MAC, EDGE_MAC)
    hub = Hub(hub_conn, config_file=hub_config, probe_swap_after=1, probe_give_up_after=4)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                session_id=SESSION_ID)
    hub._mirror_endpoint_config(endpoint, {"sf": 9, "trial": 30})
    # Consume the one-visit "fresh" skip (the mirror visit): the tests below drive real probes.
    hub._probe_visit_end(endpoint, heard=False, completed=False)
    assert endpoint.sf == 9 and hub.endpoint_trial_old(endpoint)[1] == 7
    return hub, endpoint


def test_probe_commits_on_a_full_exchange_on_the_new_config(tmp_path):
    # Located AND a full uplink completed on the new config: commit — the trial ends, the
    # endpoint stays on new.
    hub, endpoint = _armed_hub(tmp_path)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert hub.endpoint_trial_old(endpoint) is None, "a full exchange on new must commit"
    assert endpoint.sf == 9, "the committed endpoint stays on the new config"


def test_probe_holds_on_new_when_only_short_frames_heard(tmp_path):
    # Located on new but no full exchange yet (only polls): hold — do not commit (a short frame
    # is not proof a max-payload chunk lands) and do not swap (the Edge is right here).
    hub, endpoint = _armed_hub(tmp_path)

    hub._probe_visit_end(endpoint, heard=True, completed=False)

    assert hub.endpoint_trial_old(endpoint) is not None, "an un-confirmed trial must hold"
    assert endpoint.sf == 9, "a located Edge on new is not swapped away from"


def test_probe_swaps_to_old_after_a_silent_visit(tmp_path):
    # A silent visit on new: the Edge is not here. Swap the probe to the old config to look for
    # a rolled-back Edge (one probe per visit — swap, then yield, do not thrash both this visit).
    hub, endpoint = _armed_hub(tmp_path)

    hub._probe_visit_end(endpoint, heard=False, completed=False)

    assert endpoint.sf == 7, "a silent visit on new must swap the probe to old"
    assert hub.endpoint_trial_old(endpoint) is not None, "the trial is still live while probing"


def test_probe_settles_on_old_when_the_edge_is_re_acquired_there(tmp_path):
    # New silent -> swap to old; then the Edge answers on old (it self-restored). Re-acquired on
    # the last-known-good: settle there (the new config failed), trial ends, endpoint on old.
    hub, endpoint = _armed_hub(tmp_path)

    hub._probe_visit_end(endpoint, heard=False, completed=False)   # swap new->old
    assert endpoint.sf == 7
    hub._probe_visit_end(endpoint, heard=True, completed=False)    # heard on old

    assert hub.endpoint_trial_old(endpoint) is None, "re-acquiring on old settles the trial"
    assert endpoint.sf == 7, "the endpoint stays on the last-known-good config"


def test_probe_advances_at_most_once_per_visit(tmp_path):
    # Round-robin fairness: a visit advances the probe by exactly one step (here, one swap),
    # never sweeping both {new, old} in a single visit — so a rare reconfig never monopolizes
    # the poll loop and starves the Hub's other, working Edges.
    hub, endpoint = _armed_hub(tmp_path)          # on new (sf9)

    hub._probe_visit_end(endpoint, heard=False, completed=False)
    assert endpoint.sf == 7, "one silent visit swaps once, to old"

    hub._probe_visit_end(endpoint, heard=False, completed=False)
    assert endpoint.sf == 9, "the next silent visit swaps once more, back to new (not twice in one)"


def test_probe_gives_up_and_restores_old_after_total_silence(tmp_path):
    # Neither config ever answers (Edge offline): after the total budget of silent visits, give
    # up and restore the endpoint to old — the best re-contact bet — and end the trial. The
    # dequeued RF_CONFIG is NOT retried (a genuinely bad config; the backend re-issues).
    hub, endpoint = _armed_hub(tmp_path)      # give_up_after=4

    for _ in range(4):
        hub._probe_visit_end(endpoint, heard=False, completed=False)

    assert hub.endpoint_trial_old(endpoint) is None, "the trial must end after total silence"
    assert endpoint.sf == 7, "a total failure restores the endpoint to the old config"


# --- Slice H6: the same reconvergence, driven in band on an unprovisioned pair -------------

def _in_band_pair(tmp_path, edge_conn, hub_conn, **hub_kwargs):
    # An open pair with nothing provisioned, which is what selects the in-band transport. The
    # Edge carries the actuator directly: in band there is no verify gate to hang one off, and
    # a node with no actuator drops the command rather than acting on it.
    edge, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                        edge_sink=Capture_sink(), **hub_kwargs)
    edge.control_actuator = Node_Control_Actuator(edge)
    hub.resolve_endpoint_rf(endpoint)
    assert endpoint.sf == 7, "the endpoint starts on the old config"
    return edge, hub, endpoint


def test_an_in_band_retune_commits_on_both_ends_when_the_new_config_works(tmp_path):
    # What the Hub example does on an open pair, with the gated loopback standing in for the
    # radio: reach a safe boundary, command a retune with no envelope around it, then keep
    # pulling. The signed route has had this coverage since slice H5. The in-band one has been
    # pinned only as far as the Edge acknowledging and arming a trial, which is the half that
    # cannot strand a node; the half that can is everything after the acknowledgement.
    edge_conn, hub_conn = _make_gated_pair()      # no dead configs: sf9 is a good link

    hub_sink = Capture_sink()
    edge, hub, endpoint = _in_band_pair(tmp_path, edge_conn, hub_conn,
                                        reclaim_timeout=2, data_sink=hub_sink,
                                        probe_swap_after=2, probe_give_up_after=20)
    up = bytes((i * 9) % 256 for i in range(400))
    edge.datasource = _Uplink_source(edge.get_chunk_size(), "up.bin", up)

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 30},
                              name="edge-serve", daemon=True)
    server.start()
    assert hub.ask_change_rf(endpoint, {"sf": 9, "trial": 30}) is True, \
        "the Edge acknowledged, so the retune is under way on both ends"
    _run_rotation(hub, endpoint, visits=20, listening_time=1.5)
    server.join(timeout=32)
    assert not server.is_alive(), "the Edge serve loop never came home"

    assert hub.endpoint_trial_old(endpoint) is None, "the Hub trial never committed"
    assert endpoint.sf == 9, "the committed endpoint must stay on the new config"
    assert edge.connector.get_rf_config()[1] == 9, "the Edge rolled back a config that worked"
    assert not edge.sf_trial, "the Edge trial committed"
    assert ("up.bin", up) in [(n, c) for n, c, _ in hub_sink.received], \
        "the uplink that proved the new config never reached the Hub"


def test_an_in_band_retune_reconverges_on_old_when_the_new_config_is_a_dead_link(tmp_path):
    # The undo, on the transport that has no envelope to fall back on. An unsigned command is
    # the only way an open deployment can retune at all, so it is also the only way an open
    # deployment can be told to go somewhere it cannot be heard. sf9 is a dead link here: the
    # Edge applies it, hears nothing, self-restores, and the Hub's probe finds it back on sf7
    # with nobody intervening. Without this the survey mode can be bricked from radio range by
    # a single frame.
    edge_conn, hub_conn = _make_gated_pair()
    edge_conn.set_dead_rf(sf=9)
    hub_conn.set_dead_rf(sf=9)

    edge, hub, endpoint = _in_band_pair(tmp_path, edge_conn, hub_conn,
                                        reclaim_timeout=2, probe_swap_after=1,
                                        probe_give_up_after=12)
    edge.datasource = _Uplink_source(edge.get_chunk_size(), "up.bin",
                                     bytes((i * 3) % 256 for i in range(300)))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 30},
                              name="edge-serve", daemon=True)
    server.start()
    assert hub.ask_change_rf(endpoint, {"sf": 9, "trial": 1}) is True, \
        "the command landed on the old config, which is the only one that still works"
    _run_rotation(hub, endpoint, visits=25, listening_time=1.0)
    server.join(timeout=32)
    assert not server.is_alive(), "the Edge serve loop never came home"

    assert hub.endpoint_trial_old(endpoint) is None, "the Hub trial never settled"
    assert endpoint.sf == 7, "the Hub did not reconverge the endpoint on the old config"
    assert edge.connector.get_rf_config()[1] == 7, "the Edge did not self-restore to sf7"
    assert not edge.sf_trial, "the Edge trial is resolved"

"""Acceptance — v3 role reversal (open mode): Hub-commanded downlink delegation.

The Hub is the permanent authority; it never surrenders control, it *delegates*: with a
downlink file pending for an Edge it sends GRANT at a safe boundary, the Edge pulls the
file exactly like a normal transfer (roles swapped), and the Edge's final-OK hands control
straight back. Every failure converges to the home arrangement (Edge serves, Hub polls):
a lost GRANT or a dead pull ends in the Hub's reclaim timer resuming the poll, a
dual-initiator overlap ends with the Edge yielding to the Hub's poll, and a reboot lands
on the home role because no swap state is ever persisted.

Both nodes are one unified swappable object — an Edge and a Hub each carry the drive loop
and the serve loop; `current_role` picks which runs. `Source`/`Requester` survive as
deprecated aliases of the same machinery.
"""
import json
import queue
import threading
import time

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.DataSinks.DataSink import DataSink
from AlLoRa.File import AlLoRa_File

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
SESSION_ID = 42        # the Edge's sid — the session's address in both directions
HUB_OWN_SID = 7        # deliberately different, so sid-mirroring bugs surface


GRANT_KIND = 0x4
METADATA_KIND = 0x3
CHUNK_KIND = 0x2


class Filtered_loopback(Loopback_connector):
    """A loopback whose transmit drops frames of a chosen v3 kind — deterministic,
    targeted loss (vs the base class's probabilistic channel) for the coordinated-
    transition failure cases: drop exactly the GRANT, or every pull request."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drop_rules = {}     # kind_code -> frames still to drop (-1 = drop forever)

    def drop_kind(self, kind_code, count=1):
        self.drop_rules[kind_code] = count

    def transmit(self, wire):
        # v3 sid-addressed P2P framing: [sid][VT][FL][integ3] — the kind is VT's low nibble.
        kind = (wire[1] & 0x0F) if len(wire) >= 2 else None
        left = self.drop_rules.get(kind, 0)
        if left:
            if left > 0:
                self.drop_rules[kind] = left - 1
            self.dropped += 1
            return True          # "sent" fine; the channel ate it
        return super().transmit(wire)


def _make_filtered_pair():
    a_to_b, b_to_a = queue.Queue(), queue.Queue()
    edge_conn = Filtered_loopback(EDGE_MAC, inbox=b_to_a, outbox=a_to_b)
    hub_conn = Filtered_loopback(HUB_MAC, inbox=a_to_b, outbox=b_to_a)
    return edge_conn, hub_conn


class Capture_sink(DataSink):
    """A DataSink that keeps every delivered file in memory (name, bytes, reception)."""

    def __init__(self):
        self.received = []

    def consume(self, file, reception=None):
        self.received.append((file.get_name(), bytes(file.get_content()), reception))
        file.discard()


class Flaky_sink(Capture_sink):
    """A sink whose first consume fails (flash full, MQTT down) then recovers."""

    def __init__(self, failures=1):
        super().__init__()
        self.to_fail = failures
        self.failed = 0

    def consume(self, file, reception=None):
        if self.failed < self.to_fail:
            self.failed += 1
            raise RuntimeError("sink briefly down")
        super().consume(file, reception)


def _write_config(path, result_path, session_id):
    config = {
        "name": "rr",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": session_id,
        "debug": False,
        "result_path": result_path,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_pair(tmp_path, edge_conn, hub_conn, edge_sink=None, **hub_kwargs):
    edge_config = str(tmp_path / "edge.json")
    hub_config = str(tmp_path / "hub.json")
    _write_config(edge_config, str(tmp_path / "edge_results"), SESSION_ID)
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)

    sink = edge_sink if edge_sink is not None else Capture_sink()
    edge = Edge(edge_conn, config_file=edge_config, data_sink=sink)
    hub = Hub(hub_conn, config_file=hub_config, **hub_kwargs)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC,
                                active=True, session_id=SESSION_ID)
    return edge, hub, endpoint, sink


# --- the unified swappable node ----------------------------------------------

def test_edge_and_hub_start_at_home_roles(tmp_path):
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, _, _ = _make_pair(tmp_path, edge_conn, hub_conn)

    # Home roles: an Edge serves (source), a Hub drives (collector).
    assert edge.home_role == "source"
    assert edge.current_role == "source"
    assert hub.home_role == "collector"
    assert hub.current_role == "collector"

    # One unified node: both types carry both whole-file loops.
    for node in (edge, hub):
        assert callable(node.listen_to_endpoint)   # drive
        assert callable(node.send_file)            # serve


def test_legacy_names_are_aliases_of_the_unified_node(tmp_path):
    from AlLoRa.Nodes.Source import Source
    from AlLoRa.Nodes.Requester import Requester
    from AlLoRa.Nodes.Gateway import Gateway

    assert issubclass(Source, Edge)
    assert issubclass(Requester, Hub)
    assert issubclass(Gateway, Requester)


def test_dead_sleep_param_refused_by_hub_swallowed_by_shims(tmp_path):
    # NEXT_ACTION_TIME_SLEEP has been a silent no-op since v2.0, when the adaptive
    # sleep controller (now Pacing) replaced the fixed inter-request gap. The new
    # surface refuses it — accepting a dead knob is a lie — while the deprecated
    # shims keep accepting and ignoring it (exactly what v2.0 did), so fielded
    # main.py files construct unchanged. Runtime tuning lives on Pacing.
    from AlLoRa.Nodes.Requester import Requester
    from AlLoRa.Nodes.Gateway import Gateway

    config = str(tmp_path / "hub.json")
    _write_config(config, str(tmp_path / "results"), HUB_OWN_SID)
    conn, _ = Loopback_connector.create_pair(HUB_MAC, EDGE_MAC)

    with pytest.raises(TypeError):
        Hub(conn, config_file=config, NEXT_ACTION_TIME_SLEEP=0.1)

    requester = Requester(conn, config_file=config, NEXT_ACTION_TIME_SLEEP=0.1)
    assert requester.pacing.sleep == requester.pacing.min_sleep   # seed untouched

    gateway = Gateway(conn, config_file=config, NEXT_ACTION_TIME_SLEEP=0.1,
                      nodes_file=str(tmp_path / "no_nodes.json"))
    assert gateway.pacing.sleep == gateway.pacing.min_sleep


# --- happy path: downlink byte-exact, then control returns to the Hub --------

def test_downlink_delivers_byte_exact_and_control_returns(tmp_path):
    payload = bytes((i * 7) % 256 for i in range(1000))   # 5 chunks at 243
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)

    hub.queue_downlink(endpoint, AlLoRa_File(name="model.bin",
                                             content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    assert hub.downlink_pending(endpoint)

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 16},
                              name="edge-serve", daemon=True)
    server.start()

    hub.listen_to_endpoint(endpoint, listening_time=12, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive(), "edge serve loop did not come home"

    # The delegated pull delivered the exact bytes to the Edge's downlink sink.
    assert [(n, c) for n, c, _ in sink.received] == [("model.bin", payload)]

    # Control returned: a post-downlink poll succeeded (the endpoint advanced past OK)
    # and both nodes are back at their home roles with nothing left pending.
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# --- reclaim on lost GRANT ---------------------------------------------------

def test_reclaim_on_lost_grant_then_redelivery(tmp_path):
    payload = bytes(i % 256 for i in range(500))   # 3 chunks at 243
    edge_conn, hub_conn = _make_filtered_pair()
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=2)

    hub.queue_downlink(endpoint, AlLoRa_File(name="cfg.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    hub_conn.drop_kind(GRANT_KIND, count=1)   # the first GRANT is lost in the channel

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 18},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=14, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    assert hub_conn.dropped == 1, "the GRANT drop never happened — filter inert"
    # The dead delegation ended in the reclaim timer (the Edge, never granted, stayed
    # home), the file stayed queued, and a later boundary re-granted and delivered it.
    assert [(n, c) for n, c, _ in sink.received] == [("cfg.bin", payload)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


def test_downlink_queued_after_first_contact_still_delegates(tmp_path):
    # A bridge-realistic sequence: the Hub is already past its first poll — the endpoint
    # sits in the idle REQUEST_DATA loop — when the downlink arrives. The safe boundary
    # is any idle point between files (never mid-chunk), not just the pre-contact OK state.
    payload = bytes((i * 3) % 256 for i in range(500))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()

    # First: nothing queued. The Hub connects and idles asking for metadata.
    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE

    # Now the downlink shows up: the next idle boundary must still delegate.
    hub.queue_downlink(endpoint, AlLoRa_File(name="late.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    hub.listen_to_endpoint(endpoint, listening_time=10, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    assert [(n, c) for n, c, _ in sink.received] == [("late.bin", payload)]
    assert not hub.downlink_pending(endpoint)


# --- reclaim on lost first pull ----------------------------------------------

def test_reclaim_on_lost_first_pull_then_recovery(tmp_path):
    payload = bytes((i * 5) % 256 for i in range(500))
    edge_conn, hub_conn = _make_filtered_pair()
    # The GRANT lands, but the Edge's first pulls die in the channel: the Hub serves
    # silence, so only its reclaim timer can end the delegation. The file must stay
    # pending, and a later delegation completes once the channel heals.
    edge_conn.drop_kind(METADATA_KIND, count=4)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=1.0)

    hub.queue_downlink(endpoint, AlLoRa_File(name="relay.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    first_swap_id = hub._swap_id

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 22},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=18, save_file=True)
    server.join(timeout=12)
    assert not server.is_alive()

    assert edge_conn.dropped >= 4, "the pull drops never happened — filter inert"
    # The reclaim timer fired at least once: delivery needed more than one GRANT
    # (the counter starts at a random byte, so count relative to it, mod 256).
    assert (hub._swap_id - first_swap_id) & 0xFF >= 2
    assert [(n, c) for n, c, _ in sink.received] == [("relay.bin", payload)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# --- fielded firmware: the legacy set_file + send_file loop ------------------

def test_grant_honored_from_legacy_send_file_loop(tmp_path):
    # Deployed Edges run `while True: set_file(); send_file()` (the v3_hello main.py)
    # and never call Edge.serve — a queued downlink must reach them anyway, or
    # queue_downlink against fielded firmware is a guaranteed permanent outage.
    down = bytes((i * 19) % 256 for i in range(500))
    up = bytes((i * 23) % 256 for i in range(500))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub_sink = Capture_sink()
    edge, hub, endpoint, edge_sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                                reclaim_timeout=2, data_sink=hub_sink)

    hub.queue_downlink(endpoint, AlLoRa_File(name="down.bin", content=bytearray(down),
                                             chunk_size=hub.get_chunk_size()))

    def fielded_main():
        # The deployed loop verbatim (bounded for the test): serve the uplink,
        # re-arm on completion. No serve(), no explicit GRANT handling.
        deadline = time.time() + 25
        while not edge_sink.received and time.time() < deadline:
            if not edge.got_file():
                edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(up),
                                          chunk_size=edge.get_chunk_size()))
            edge.send_file(timeout=5)       # seconds

    server = threading.Thread(target=fielded_main, name="edge-legacy", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=30)
    assert not server.is_alive()

    assert [(n, c) for n, c, _ in edge_sink.received] == [("down.bin", down)]
    assert ("up.bin", up) in [(n, c) for n, c, _ in hub_sink.received]
    assert not hub.downlink_pending(endpoint)


# --- an undeliverable downlink must not starve the uplink --------------------

def test_undeliverable_downlink_does_not_starve_uplink_polling(tmp_path):
    # Every GRANT dies in the channel (a deaf or dead Edge): the delegation must
    # back off instead of turning every drive round into GRANT + reclaim silence,
    # or one bad queue_downlink means a permanent uplink outage for that Edge.
    edge_conn, hub_conn = _make_filtered_pair()
    hub_conn.drop_kind(GRANT_KIND, count=-1)     # no GRANT ever arrives
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=0.5)
    hub.queue_downlink(endpoint, AlLoRa_File(name="dead.bin",
                                             content=bytearray(bytes(300)),
                                             chunk_size=hub.get_chunk_size()))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    # The uplink stayed alive: the Edge's poll was asked and answered.
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE
    # And the futile delegations were capped + backed off, not fired every round.
    assert 1 <= hub_conn.dropped <= 5
    # The file is still queued for a later, healthier boundary (at-least-once).
    assert hub.downlink_pending(endpoint)


def test_failed_delegations_do_not_count_as_pacing_successes(tmp_path):
    # The inter-request sleep controller hunts for the shortest gap the LINK
    # tolerates; a delegation round that served nobody says nothing about that
    # gap. Against a fully silent peer, nothing may register as a success.
    edge_conn, hub_conn = _make_filtered_pair()
    hub_conn.drop_kind(GRANT_KIND, count=-1)
    _, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                     reclaim_timeout=0.3)
    hub.queue_downlink(endpoint, AlLoRa_File(name="dead.bin",
                                             content=bytearray(bytes(300)),
                                             chunk_size=hub.get_chunk_size()))

    hub.listen_to_endpoint(endpoint, listening_time=4, save_file=True)

    assert hub.pacing.successful_interactions_count == 0


# --- a file object is servable more than once --------------------------------

def test_requeued_downlink_delivers_again(tmp_path):
    # Broadcast reuses one AlLoRa_File across endpoints, and a config re-push
    # re-queues an already-delivered artifact. The second delegation must serve
    # the file again — the sent flag from the first delivery must not turn it
    # into an instant phantom "delivered" that the Edge never receives.
    payload = bytes((i * 17) % 256 for i in range(500))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)
    artifact = AlLoRa_File(name="cfg.bin", content=bytearray(payload),
                           chunk_size=hub.get_chunk_size())

    for round_number in (1, 2):
        hub.queue_downlink(endpoint, artifact)
        server = threading.Thread(target=edge.serve, kwargs={"timeout": 16},
                                  name="edge-serve", daemon=True)
        server.start()
        hub.listen_to_endpoint(endpoint, listening_time=12, save_file=True)
        server.join(timeout=10)
        assert not server.is_alive()
        assert [(n, c) for n, c, _ in sink.received] \
            == [("cfg.bin", payload)] * round_number
        assert not hub.downlink_pending(endpoint)


# --- at-least-once: the sink is fed before the final-OK flies ----------------

def test_downlink_survives_a_failing_sink_consume(tmp_path):
    # The final-OK retires the file on the serving side (and pops the Hub's downlink
    # queue), so it must not fly until the sink actually has the file. A sink that
    # fails once (flash full, MQTT down) leaves the transfer unacknowledged; the next
    # round pulls the file again and the retry delivers it. At-least-once delivery:
    # sinks are idempotent, a lost final-OK may at worst feed one twice.
    payload = bytes((i * 13) % 256 for i in range(500))   # 3 chunks at 243
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           edge_sink=Flaky_sink(failures=1),
                                           reclaim_timeout=3)

    hub.queue_downlink(endpoint, AlLoRa_File(name="retry.bin",
                                             content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=12)
    assert not server.is_alive()

    assert sink.failed == 1, "the sink failure never happened — test inert"
    assert [(n, c) for n, c, _ in sink.received] == [("retry.bin", payload)]
    assert not hub.downlink_pending(endpoint)
    assert hub.current_role == "collector"
    assert edge.current_role == "source"


# --- Hub-authority tie-break -------------------------------------------------

def test_hub_authority_tie_break_edge_yields_to_poll(tmp_path):
    # A dual-initiator overlap, forced the realistic way: the Hub "rebooted" right after
    # a GRANT went out — it lost the delegation (empty queue, home role) and just polls —
    # while the Edge honors that GRANT and starts driving. Hub always wins: the Edge must
    # yield the instant its pull hears the poll, and answer it from home.
    from AlLoRa.Packet_v3 import Packet_v3

    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn)

    # The stale GRANT, exactly as the pre-reboot Hub framed it.
    grant = Packet_v3(addressing="sid")
    grant.set_session(SESSION_ID)
    grant.set_grant(9)
    hub_conn.transmit(grant.get_content())

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=8)
    assert not server.is_alive(), "edge never yielded — it sat out the whole drive window"

    assert edge.yield_count >= 1
    assert sink.received == []               # nothing was ever there to pull
    assert edge.current_role == "source"
    assert hub.current_role == "collector"
    # The poll landed after the yield: the endpoint advanced past its pre-contact state.
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE


# --- GRANT dedupe: ids retire on completion, and a reboot can't collide ------

def test_regrant_after_failed_pull_is_honored(tmp_path):
    # Only a COMPLETED pull retires a delegation id. An Edge whose granted pull died
    # (its Hub rebooted mid-delegation and never served it) must honor the same id
    # again — a rebooted Hub can legitimately re-grant with it.
    from AlLoRa.Packet_v3 import Packet_v3

    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn)
    edge.downlink_window = 1     # a dead pull gives up quickly (test pacing)

    grant = Packet_v3(addressing="sid")
    grant.set_session(SESSION_ID)
    grant.set_grant(9)
    hub_conn.transmit(grant.get_content())
    edge.serve(timeout=4)                     # honors GRANT(9); nobody serves; dies
    assert sink.received == []

    while not edge_conn.outbox.empty():       # drop attempt 1's pull requests
        edge_conn.outbox.get_nowait()

    hub_conn.transmit(grant.get_content())    # the re-GRANT carries the same id
    edge.serve(timeout=4)

    assert not edge_conn.outbox.empty(), \
        "the Edge never pulled again — the failed delegation retired its id"


def test_duplicate_grant_after_completed_pull_is_ignored(tmp_path):
    # The counterpart pin: after a COMPLETED pull, a stray duplicate of the same
    # GRANT must be ignored — no second pull, no wasted drive window.
    from AlLoRa.Packet_v3 import Packet_v3

    payload = bytes((i * 29) % 256 for i in range(300))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)
    hub.queue_downlink(endpoint, AlLoRa_File(name="one.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    server = threading.Thread(target=edge.serve, kwargs={"timeout": 14},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=10, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()
    assert [(n, c) for n, c, _ in sink.received] == [("one.bin", payload)]

    while not edge_conn.outbox.empty():
        edge_conn.outbox.get_nowait()

    duplicate = Packet_v3(addressing="sid")
    duplicate.set_session(SESSION_ID)
    duplicate.set_grant(hub._swap_id)         # the id the completed pull used
    hub_conn.transmit(duplicate.get_content())
    edge.serve(timeout=3)

    assert edge_conn.outbox.empty(), "the Edge pulled again on a duplicate GRANT"


def test_rebooted_hub_grants_do_not_resume_at_a_fixed_id(tmp_path, monkeypatch):
    # A reboot restarts the Hub's GRANT counter. If it always restarted at the same
    # value, an Edge remembering the pre-reboot id would silently drop the first
    # post-reboot GRANT — so the counter starts at a random byte.
    from AlLoRa.Packet_v3 import Packet_v3
    import AlLoRa.Nodes.Hub as hub_module

    monkeypatch.setattr(hub_module, "urandom", lambda n: bytes([0x2A]))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    _, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                     reclaim_timeout=0.3)
    hub.queue_downlink(endpoint, AlLoRa_File(name="dl.bin",
                                             content=bytearray(bytes(300)),
                                             chunk_size=hub.get_chunk_size()))
    hub.listen_to_endpoint(endpoint, listening_time=1, save_file=True)

    grant_frame = edge_conn.inbox.get_nowait()   # the Hub's first frame is the GRANT
    packet = hub.connector.codec.deframe(grant_frame)
    assert packet.get_command() == Packet_v3.GRANT
    assert packet.get_swap_id() == 0x2B          # seeded 0x2A, first grant is +1


# --- an abandoned pull must not leak its reassembly buffer -------------------

def test_abandoned_pull_discards_its_reassembly_buffer(tmp_path):
    # A granted pull that dies mid-file (every chunk request lost, window expires)
    # must close and remove its reassembly buffer on the way home: each retry opens
    # a fresh one, and an ESP32's FD table is small enough that leaking one per
    # attempt eventually kills the node.
    payload = bytes((i * 31) % 256 for i in range(500))
    edge_conn, hub_conn = _make_filtered_pair()
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=1.0)
    edge.downlink_window = 2                  # give up the dead pull quickly
    edge_conn.drop_kind(CHUNK_KIND, count=-1)  # METADATA lands; every chunk req dies

    hub.queue_downlink(endpoint, AlLoRa_File(name="fw.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))

    hub_thread = threading.Thread(
        target=hub.listen_to_endpoint,
        kwargs=dict(digital_endpoint=endpoint, listening_time=8, save_file=True),
        name="hub-drive", daemon=True)
    hub_thread.start()
    edge.serve(timeout=6)
    hub_thread.join(timeout=12)
    assert not hub_thread.is_alive()

    assert sink.received == []                 # nothing ever completed
    assert hub.downlink_pending(endpoint)      # the file is still queued
    temp = tmp_path / "edge_results" / "hub" / "Temp" / "fw.bin.tmp"
    assert not temp.exists(), "the dead pull leaked its reassembly buffer"


# --- windows spanning the MicroPython tick wrap ------------------------------

def _install_wrapping_clock(monkeypatch, wrap_in_ms=1000):
    # Replace the node modules' millisecond clock with MicroPython-like ticks that
    # hit the 2^30 wrap `wrap_in_ms` from now (advancing with real time).
    import AlLoRa.Nodes.Hub as hub_module
    import AlLoRa.Nodes.Edge as edge_module
    import AlLoRa.Nodes.Swap_base as swap_module

    base = time.time() * 1000
    start = (1 << 30) - wrap_in_ms

    def fake_ticks():
        return int(start + time.time() * 1000 - base) % (1 << 30)

    for module in (hub_module, edge_module, swap_module):
        monkeypatch.setattr(module, "time", fake_ticks)


def test_drive_and_serve_windows_survive_the_ticks_wrap(tmp_path, monkeypatch):
    # utime.ticks_ms wraps at 2^30 ms (~12.4 days). A 4 s window that spans the
    # wrap must still end on time — with raw arithmetic it holds for up to ~12
    # MORE days (a Hub stuck serving, an Edge stuck in its loop), which no field
    # deployment survives. Observed here as loops that never come home.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=1)
    _install_wrapping_clock(monkeypatch)      # ticks wrap ~1 s into the windows

    hub_thread = threading.Thread(
        target=hub.listen_to_endpoint,
        kwargs=dict(digital_endpoint=endpoint, listening_time=4, save_file=True),
        name="hub-drive", daemon=True)
    edge_thread = threading.Thread(target=edge.serve, kwargs={"timeout": 4},
                                   name="edge-serve", daemon=True)
    hub_thread.start()
    edge_thread.start()
    hub_thread.join(timeout=12)
    edge_thread.join(timeout=12)

    assert not hub_thread.is_alive(), "the drive window spanned the wrap and never ended"
    assert not edge_thread.is_alive(), "the serve loop spanned the wrap and never ended"


# --- reboot to home role -----------------------------------------------------

def test_edge_reboot_mid_swap_lands_home_and_reconverges(tmp_path):
    # No swap state is ever persisted: an Edge that dies mid-pull comes back at its home
    # serve role, and the Hub's reclaim + re-GRANT still deliver the pending file.
    payload = bytes((i * 11) % 256 for i in range(500))
    edge_conn, hub_conn = _make_filtered_pair()
    edge1, hub, endpoint, sink1 = _make_pair(tmp_path, edge_conn, hub_conn,
                                             reclaim_timeout=1.2)
    edge1.downlink_window = 1.5   # give up a dead pull quickly (test pacing)

    hub.queue_downlink(endpoint, AlLoRa_File(name="fw.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))

    # Edge1 gets the GRANT and pulls METADATA, then its drive dies (every CHUNK request
    # is lost) until "power-off": serve() returns and the instance is abandoned.
    edge_conn.drop_kind(CHUNK_KIND, count=-1)
    hub_thread = threading.Thread(
        target=hub.listen_to_endpoint,
        kwargs=dict(digital_endpoint=endpoint, listening_time=25, save_file=True),
        name="hub-drive", daemon=True)
    hub_thread.start()
    edge1.serve(timeout=3)
    assert edge1.current_role == "source"    # it came home before "dying"
    assert sink1.received == []

    # Reboot: the channel heals and a fresh Edge starts on the same radio. Home role,
    # no memory of the granted pull.
    edge_conn.drop_rules.clear()
    sink2 = Capture_sink()
    edge2 = Edge(edge_conn, config_file=str(tmp_path / "edge.json"), data_sink=sink2)
    assert edge2.current_role == "source" == edge2.home_role

    edge2.serve(timeout=14)
    hub_thread.join(timeout=20)
    assert not hub_thread.is_alive()

    # The Hub's resumed drive re-granted and the new Edge delivered the file intact.
    assert [(n, c) for n, c, _ in sink2.received] == [("fw.bin", payload)]
    assert hub.current_role == "collector"
    assert edge2.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# --- return to normal: downlink, then a regular uplink in the same session ---

def test_uplink_still_normal_after_downlink_reversal(tmp_path):
    down = bytes(i % 256 for i in range(700))          # 3 chunks at 243
    up = bytes((255 - i) % 256 for i in range(600))    # 3 chunks at 243
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub_sink = Capture_sink()
    edge, hub, endpoint, edge_sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                                reclaim_timeout=3, data_sink=hub_sink)

    hub.queue_downlink(endpoint, AlLoRa_File(name="down.bin", content=bytearray(down),
                                             chunk_size=hub.get_chunk_size()))
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(up),
                              chunk_size=edge.get_chunk_size()))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    # Down first (the boundary delegation), then the normal uplink — each exactly once,
    # each byte-exact, each to its own side's sink.
    assert [(n, c) for n, c, _ in edge_sink.received] == [("down.bin", down)]
    assert [(n, c) for n, c, _ in hub_sink.received] == [("up.bin", up)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)

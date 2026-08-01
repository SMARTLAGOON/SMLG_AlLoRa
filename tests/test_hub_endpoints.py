"""A Hub holds 1+ endpoints and owns the visit loop that polls them.

That story used to live one level down, in `Gateway`: the Hub had no endpoint collection and
no loop at all, so the single-endpoint and multi-endpoint collectors were different classes
rather than the same class with one or many peers. These tests pin the surface on `Hub`,
where the loop is now the node's `run()` verb rather than a method named after a check.

They also pin the visit cadence, which never worked. The reschedule added `asking_frequency`
(seconds) to a millisecond clock, so an endpoint configured to be polled every 5 minutes was
polled 300 ms after its last visit: the knob was off by a factor of 1000 and every fielded
gateway round-robined continuously. And the deadlines were raw `+` and `>=` on a counter that
wraps at 2^30 ms, so a gateway crossing the wrap would have parked an endpoint for ~12 days.
The clock here is fake and wraps like MicroPython's, so both are testable without waiting.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes import Hub as hub_module
from AlLoRa.Nodes.Gateway import Gateway
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"
_TICKS_PERIOD = 1 << 30


def _write_config(path, result_path):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 9,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _write_nodes(path, nodes):
    with open(path, "w") as f:
        json.dump(nodes, f)


def _node(name, mac, **overrides):
    node = {"name": name, "mac_address": mac, "active": True,
            "asking_frequency": 60, "listening_time": 30}
    node.update(overrides)
    return node


def _make_hub(tmp_path, nodes=None, **kwargs):
    config = str(tmp_path / "hub.json")
    _write_config(config, str(tmp_path / "results"))
    if nodes is not None:
        nodes_file = str(tmp_path / "Nodes.json")
        _write_nodes(nodes_file, nodes)
        kwargs.setdefault("nodes_file", nodes_file)
    return Hub(Loopback_connector(HUB_MAC), config_file=config, **kwargs)


class _Clock:
    """A millisecond counter that wraps exactly like MicroPython's `ticks_ms`."""

    def __init__(self, start=0):
        self.now = start % _TICKS_PERIOD

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now = (self.now + int(ms)) % _TICKS_PERIOD


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(hub_module, "time", c)
    # Always advance by at least a tick: a pacing sleep of 0 would otherwise let the loop
    # spin without the bounded run ever reaching its deadline.
    monkeypatch.setattr(hub_module, "sleep", lambda s: c.advance_ms(max(1, s * 1000)))
    return c


def _record_visits(hub, clock, visits, on_visit=None):
    """Stand in for the radio: log the visit, burn the listening window on the fake clock."""
    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False):
        visits.append((digital_endpoint.get_name(), listening_time))
        clock.advance_ms((listening_time or 0) * 1000)
        if on_visit is not None:
            on_visit(digital_endpoint)
        return True

    hub.listen_to_endpoint = listen


# --- registration ------------------------------------------------------------------------

def test_hub_registers_the_active_endpoints_from_the_nodes_file(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1"),
                               _node("edge-b", "b1b1b1b1", active=False),
                               _node("edge-c", "c1c1c1c1")])

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["edge-a", "edge-c"]


def test_hub_assigns_a_unique_session_id_per_endpoint(tmp_path):
    # Two endpoints registered by device_id sharing a first byte derive the same sid; the
    # Hub has to break the clash at registration, before either is addressed.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", device_id="aa11223344556677"),
                               _node("edge-b", "b1b1b1b1", device_id="aa8899aabbccddee")])

    sids = [ep.session_id for ep in hub.digital_endpoints]
    assert len(set(sids)) == 2


def test_a_hub_with_no_nodes_file_registers_nothing_and_still_builds(tmp_path):
    hub = _make_hub(tmp_path, nodes_file=str(tmp_path / "absent.json"))

    assert hub.digital_endpoints == []


def test_set_digital_endpoints_replaces_the_collection_and_its_status_map(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])

    hub.set_digital_endpoints([Digital_Endpoint(config=_node("edge-z", "f1f1f1f1"))])

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["edge-z"]
    assert list(hub.status["Digital_Endpoints"]) == ["f1f1f1f1"]


# --- the visit loop ----------------------------------------------------------------------

def test_run_visits_every_registered_endpoint(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=1),
                               _node("edge-b", "b1b1b1b1", listening_time=1),
                               _node("edge-c", "c1c1c1c1", listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=10)

    assert {name for name, _ in visits} == {"edge-a", "edge-b", "edge-c"}


def test_run_listens_for_the_endpoints_configured_window(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=7)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=5)

    assert visits[0] == ("edge-a", 7)


def test_asking_frequency_paces_the_visits_in_seconds(tmp_path, clock):
    # The bug this pins: `asking_frequency` was added to a millisecond clock, so a 300 s
    # endpoint came back up 300 ms later and the knob did nothing at all.
    hub = _make_hub(tmp_path, [_node("slow", "a1a1a1a1", asking_frequency=300,
                                     listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=100)

    assert len(visits) == 1


def test_a_due_endpoint_is_revisited_within_the_same_run(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("fast", "a1a1a1a1", asking_frequency=10,
                                     listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=100)

    # Roughly 100 s of runtime at an 11 s visit-plus-gap cadence; the exact count depends on
    # the pacing sleep, so pin the order of magnitude, not the number.
    assert 5 <= len(visits) <= 12


def test_the_schedule_survives_the_tick_wrap(tmp_path, clock):
    # MicroPython's ticks_ms wraps at 2^30 ms (~12.4 days). A deadline built with raw `+`
    # lands past the wrap as a number the clock never reaches again, so the endpoint would go
    # unvisited for another full period. Start late enough that the first visit ends before
    # the wrap but its reschedule falls after it, which is the window that breaks.
    clock.now = _TICKS_PERIOD - 5000
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=10,
                                     listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=60)

    assert len(visits) >= 2


def test_run_returns_when_its_bounded_window_expires(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=1,
                                     listening_time=1)])
    started = clock.now
    _record_visits(hub, clock, [])

    hub.run(timeout=30)

    # Bounded means bounded: the loop stops near its deadline rather than overrunning by
    # another whole visit cadence.
    assert 30_000 <= (clock.now - started) < 40_000


def test_an_endpoint_that_raises_neither_stops_the_loop_nor_starves_the_others(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("broken", "a1a1a1a1", listening_time=1),
                               _node("healthy", "b1b1b1b1", listening_time=1)])
    visits = []

    def listen(digital_endpoint, listening_time=None, **kwargs):
        clock.advance_ms((listening_time or 0) * 1000)
        if digital_endpoint.get_name() == "broken":
            raise RuntimeError("radio fell over")
        visits.append(digital_endpoint.get_name())
        return True

    hub.listen_to_endpoint = listen

    hub.run(timeout=10)

    assert "healthy" in visits


def test_a_locked_endpoint_with_missing_chunks_gets_a_second_window(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=3,
                                     lock_on_file_receive=True,
                                     max_listen_time_when_locked=90)])
    visits = []

    class _PartialFile:
        def get_missing_chunks(self):
            return [4, 5]

    hub.digital_endpoints[0].set_current_file(_PartialFile())
    _record_visits(hub, clock, visits)

    hub.run(timeout=1)

    assert [window for _, window in visits][:2] == [3, 90]


def test_run_publishes_each_endpoints_reception_info_to_subscribers(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=1)])
    seen = []

    class _Subscriber:
        def update(self, status):
            seen.append(dict(status["Digital_Endpoints"]))

    hub.register_subscriber(_Subscriber())
    _record_visits(hub, clock, [])

    hub.run(timeout=5)

    assert seen, "a visit must push the endpoint map to subscribers"
    assert "a1a1a1a1" in seen[-1]


# --- the names that used to hold all of this ----------------------------------------------

def test_check_digital_endpoints_still_drives_the_loop(tmp_path, clock):
    # Fielded main.py files call the old name; it survives as an alias for run().
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.check_digital_endpoints(timeout=5)

    assert visits


def test_gateway_keeps_no_logic_of_its_own(tmp_path):
    # Gateway is now a preset over Hub, like Requester: it pins the multi-endpoint defaults
    # and the legacy argument order, and defines nothing else.
    assert set(vars(Gateway)) - {"__module__", "__qualname__", "__doc__"} == {"__init__"}

    for verb in ("run", "check_digital_endpoints", "add_digital_endpoints",
                 "set_digital_endpoints", "update_subscribers"):
        assert getattr(Gateway, verb) is getattr(Hub, verb)


def test_both_node_types_run_with_the_same_verb():
    # A deployment's main.py should not have to remember which placement it is holding:
    # Edge(...).run() and Hub(...).run() are the one way to start a node.
    from AlLoRa.Nodes.Edge import Edge

    assert callable(Edge.run) and callable(Hub.run)
    # `serve` survives on the Edge as the deprecated spelling, but it is no longer the
    # definition: it forwards, so there is a single loop to change.
    assert Edge.serve is not Edge.run


def test_a_gateway_built_the_old_way_still_registers_its_endpoints(tmp_path):
    config = str(tmp_path / "hub.json")
    nodes = str(tmp_path / "Nodes.json")
    _write_config(config, str(tmp_path / "results"))
    _write_nodes(nodes, [_node("edge-a", "a1a1a1a1")])

    gateway = Gateway(Loopback_connector(HUB_MAC), config, False, 0.1, nodes, None)

    assert [ep.get_name() for ep in gateway.digital_endpoints] == ["edge-a"]

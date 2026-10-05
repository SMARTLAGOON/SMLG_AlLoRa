"""A Hub paces each link on its own, not the radio as a whole.

A Hub visits many endpoints with one radio, and those are different links: different
distance, round trip and reliability. With one receive window and one inter-request sleep
shared across the roster, a node with nothing to send widened the window and backed the
sleep off during its own visit, and the busy node that came next started detuned and spent
its visit climbing back down. Measured on the lab Hub with a mostly idle water node and a
GNSS node holding a 2321-chunk file: 30-second visits ran at about 3.7 s per chunk and
never reached the 0.77 s the same link runs at once settled.

So the learned state belongs to the endpoint, and what is a property of the radio, the
Time-on-Air bounds, stays with the radio. The visit loop is driven here with the radio
replaced by a stand-in that feeds pacing the way a real visit does: a busy endpoint answers
every round, an idle one never does.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes import Hub as hub_module
from AlLoRa.Nodes import Node as node_module
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"


def _make_hub(tmp_path, nodes):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": 9,
        "debug": False, "result_path": str(tmp_path / "results"),
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config_file = tmp_path / "hub.json"
    config_file.write_text(json.dumps(config))
    nodes_file = tmp_path / "Nodes.json"
    nodes_file.write_text(json.dumps(nodes))
    return Hub(Loopback_connector(HUB_MAC), config_file=str(config_file),
               nodes_file=str(nodes_file))


def _node(name, mac, **overrides):
    node = {"name": name, "mac_address": mac, "active": True,
            "wait_after_visit": 0, "listening_time": 1}
    node.update(overrides)
    return node


@pytest.fixture
def clock(monkeypatch):
    now = [0]
    monkeypatch.setattr(hub_module, "time", lambda: now[0])
    monkeypatch.setattr(hub_module, "sleep", lambda s: now.__setitem__(0, now[0] + max(1, int(s * 1000))))
    monkeypatch.setattr(node_module, "sleep", lambda s: None)   # the settle after a retune
    return now


ROUND_TRIP = 0.4    # seconds; what a settled SF7 link answers in, well inside the bounds


def _drive_visits(hub, clock, busy, rounds=20):
    """Stand in for the radio. Each visit runs `rounds` request rounds; the endpoints named
    in `busy` answer every one, the rest answer none. Records, per visit, the window and the
    sleep the visit started with and the ones it ended with."""
    log = []

    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False, stall_timeout=None):
        connector = hub.connector
        hub.prepare_connector(digital_endpoint)     # what a real visit does first
        start = (connector.adaptive_timeout, hub.NEXT_ACTION_TIME_SLEEP)
        for _ in range(rounds):
            if digital_endpoint.get_name() in busy:
                connector.decrease_adaptive_timeout(ROUND_TRIP)
                hub.pacing.on_success()
            else:
                connector.increase_adaptive_timeout()
                hub.pacing.on_failure()
        end = (connector.adaptive_timeout, hub.NEXT_ACTION_TIME_SLEEP)
        log.append((digital_endpoint.get_name(), start, end))
        clock[0] += 1000
        return True

    hub.listen_to_endpoint = listen
    return log


def test_an_idle_endpoint_does_not_detune_the_busy_one(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("gnss", "a1a1a1a1"), _node("water", "b1b1b1b1")])
    log = _drive_visits(hub, clock, busy={"gnss"})

    hub.run(timeout=8)

    gnss = [(start, end) for name, start, end in log if name == "gnss"]
    assert len(gnss) >= 2 and any(name == "water" for name, _, _ in log)
    # Every gnss visit after the first picks up exactly where the previous one left off,
    # although a water visit, all timeouts and failures, ran in between.
    for (_, previous_end), (next_start, _) in zip(gnss, gnss[1:]):
        assert next_start == previous_end


def test_a_link_keeps_what_it_learned_across_the_retune_to_another_peer(tmp_path, clock):
    # Two peers on different spreading factors: every visit retunes the radio. The window
    # bounds follow the radio, but retuning back to a link's own config is not news about
    # that link, so what it learned there must survive the round trip through the other's.
    hub = _make_hub(tmp_path, [_node("gnss", "a1a1a1a1"), _node("water", "b1b1b1b1", sf=9)])
    log = _drive_visits(hub, clock, busy={"gnss", "water"})

    hub.run(timeout=8)

    gnss = [(start, end) for name, start, end in log if name == "gnss"]
    assert len(gnss) >= 2 and any(name == "water" for name, _, _ in log)
    for (_, previous_end), (next_start, _) in zip(gnss, gnss[1:]):
        assert next_start == previous_end

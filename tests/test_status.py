"""The observability surface: the live status values plus whoever is watching them.

It used to be defined on `Node`, so anything that wanted a screen or a logger had to inherit a
node even when it ran no protocol at all. These tests pin the surface on its own, and pin what
a subscriber receives, which until now was only ever exercised through a running node.
"""
import json

from AlLoRa.Status import Status
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


class Recorder:
    """Whatever a subscriber does with the values, this is the contract it sees."""

    def __init__(self):
        self.seen = []

    def update(self, status):
        self.seen.append(status)


def _make_edge(tmp_path):
    config_path = str(tmp_path / "edge.json")
    config = {
        "name": "watched",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": 42,
        "debug": False,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(config_path, "w") as f:
        json.dump(config, f)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=config_path)


# --- the surface on its own ------------------------------------------------------------

def test_a_non_node_can_be_observed():
    # No connector, no config file, no node: a bridge board holds one of these to drive a
    # screen while serving the channel.
    status = Status()
    watcher = Recorder()
    status.register(watcher)

    status["RSSI"] = -42
    status.notify()

    assert watcher.seen == [{"RSSI": -42}]


def test_subscribers_receive_a_plain_dict():
    # The monitoring layer does `status.get(...)`, `key in status` and json.dumps over what
    # it is handed, so the object that crosses the seam stays an ordinary dict.
    status = Status()
    watcher = Recorder()
    status.register(watcher)
    status["SF"] = 12

    status.notify()

    payload = watcher.seen[0]
    assert type(payload) is dict
    assert payload.get("SF") == 12
    assert "SF" in payload
    assert json.loads(json.dumps(payload)) == {"SF": 12}


def test_the_handed_over_dict_stays_live():
    # The OLED screen keeps the reference it was given and re-reads it on every refresh, so
    # the same dict has to be updated in place rather than replaced per notification.
    status = Status()
    watcher = Recorder()
    status.register(watcher)

    status["Chunk"] = 1
    status.notify()
    status["Chunk"] = 2
    status.notify()

    first, second = watcher.seen
    assert first is second
    assert first["Chunk"] == 2


def test_values_can_be_read_and_updated_in_place():
    # The node loops count with `status['Retransmission'] += 1` and reach into a nested map.
    status = Status()
    status["Retransmission"] = 0
    status["Digital_Endpoints"] = {}

    status["Retransmission"] += 1
    status["Digital_Endpoints"]["a1a1a1a1"] = {"total_chunks": 3}

    assert status["Retransmission"] == 1
    assert status["Digital_Endpoints"] == {"a1a1a1a1": {"total_chunks": 3}}


def test_registering_twice_notifies_once():
    status = Status()
    watcher = Recorder()

    status.register(watcher)
    status.register(watcher)
    status.notify()

    assert len(watcher.seen) == 1


def test_unregister_stops_the_updates():
    status = Status()
    watcher = Recorder()
    status.register(watcher)

    status.unregister(watcher)
    status.notify()

    assert watcher.seen == []


def test_unregistering_a_stranger_is_a_no_op():
    status = Status()
    watcher = Recorder()
    status.register(watcher)

    status.unregister(Recorder())   # never registered
    status.notify()

    assert len(watcher.seen) == 1


def test_notifying_with_nobody_watching_is_a_no_op():
    # The loops guard on `status.subscribers` to skip the bookkeeping when nobody is
    # listening, so the empty case has to be cheap and silent, not an error.
    status = Status()
    assert not status.subscribers
    status.notify()


# --- what a node still hands to the monitoring layer ------------------------------------

def test_a_node_reports_the_same_payload_as_before(tmp_path):
    edge = _make_edge(tmp_path)
    watcher = Recorder()

    edge.register_subscriber(watcher)
    edge.notify_subscribers()

    payload = watcher.seen[0]
    assert type(payload) is dict
    assert payload["MAC"] == EDGE_MAC
    assert payload["SF"] == 7
    assert payload["Freq"] == 868
    assert payload["Status"] == "WAIT"
    assert payload["Retransmission"] == 0
    assert payload["CorruptedPackets"] == 0
    for key in ("BW", "CR", "TX_P", "RSSI", "SNR", "Chunk", "File",
                "PSizeS", "PSizeR", "TimePS", "TimePR", "TimeBtw"):
        assert key in payload


def test_a_node_unregisters_its_subscriber(tmp_path):
    edge = _make_edge(tmp_path)
    watcher = Recorder()
    edge.register_subscriber(watcher)

    edge.unregister_subscriber(watcher)
    edge.notify_subscribers()

    assert watcher.seen == []

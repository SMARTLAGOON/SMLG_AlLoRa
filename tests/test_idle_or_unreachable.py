"""An Edge with nothing to send and an Edge that is gone are the same silence on the air.

An Edge with no file lets a METADATA request go by unanswered rather than spend airtime saying
"nothing yet", so a visit to an idle sensor and a visit to a cut cable leave the same lines in
the Hub's journal. On 2026-09-30 that sent a diagnosis toward a crashed node when the fault
was the cable. The silence has to be asked about rather than read: the Hub ends such a visit
with the connection poll, the one request an Edge always answers, and says which it was.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


class Status_log:
    """A subscriber that keeps every state the node reported, in order."""

    def __init__(self):
        self.states = []

    def update(self, status):
        self.states.append(status.get("Status"))


def _write_config(path, result_path, debug=False):
    config = {
        "name": "liveness",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "debug": debug,
        "result_path": result_path,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _hub(tmp_path, hub_conn, debug=False):
    config = str(tmp_path / "hub.json")
    _write_config(config, str(tmp_path / "hub_results"), debug=debug)
    hub = Hub(hub_conn, config_file=config)
    log = Status_log()
    hub.register_subscriber(log)
    endpoint = Digital_Endpoint(name="water-quality", mac_address=EDGE_MAC, active=True)
    return hub, endpoint, log


def _idle_edge(tmp_path, edge_conn, serve_for):
    config = str(tmp_path / "edge.json")
    _write_config(config, str(tmp_path / "edge_results"))
    edge = Edge(edge_conn, config_file=config)
    server = threading.Thread(target=edge.serve, kwargs={"timeout": serve_for},
                              name="edge-serve", daemon=True)
    server.start()
    return server


def test_a_visit_to_an_edge_with_nothing_to_send_ends_idle(tmp_path):
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub, endpoint, log = _hub(tmp_path, hub_conn)
    server = _idle_edge(tmp_path, edge_conn, serve_for=10)

    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)
    server.join(timeout=15)

    assert log.states[-1] == "idle", log.states[-5:]


def test_a_wholly_silent_visit_to_an_idle_edge_is_asked_about_and_ends_idle(tmp_path):
    # The case from the field: first contact long done, so every request of the visit is a
    # METADATA request, and an Edge with nothing to send answers none of them.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub, endpoint, log = _hub(tmp_path, hub_conn)
    server = _idle_edge(tmp_path, edge_conn, serve_for=15)

    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)   # first contact
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE
    del log.states[:]
    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)   # METADATA only
    server.join(timeout=20)

    assert log.states[-1] == "idle", log.states[-5:]


def test_a_silent_visit_to_an_edge_that_is_gone_ends_unreachable(tmp_path):
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub, endpoint, log = _hub(tmp_path, hub_conn)
    server = _idle_edge(tmp_path, edge_conn, serve_for=4)

    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)   # first contact
    server.join(timeout=10)                                              # then it is gone
    assert not server.is_alive()
    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)

    assert log.states[-1] == "unreachable", log.states[-5:]


def test_an_edge_never_reached_ends_unreachable(tmp_path):
    # Before first contact every request of the visit already is the connection poll, so its
    # silence is the answer and no extra question is owed.
    _, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub, endpoint, log = _hub(tmp_path, hub_conn)

    hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)

    assert endpoint.state == Digital_Endpoint.OK
    assert log.states[-1] == "unreachable", log.states[-5:]


def test_the_journal_names_the_node_and_says_which(tmp_path, capsys):
    # What the person reading the journal greps for is the node's name, so the verdict sits on
    # the same line as it.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub, endpoint, _ = _hub(tmp_path, hub_conn, debug=True)
    server = _idle_edge(tmp_path, edge_conn, serve_for=4)

    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)
    server.join(timeout=10)
    hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)

    out = capsys.readouterr().out
    idle = out.find("water-quality ({}): idle, nothing to send\n".format(EDGE_MAC))
    gone = out.find("water-quality ({}): unreachable\n".format(EDGE_MAC))
    assert 0 <= idle < gone, [l for l in out.splitlines() if "water-quality (" in l]

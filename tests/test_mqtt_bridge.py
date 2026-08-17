"""Acceptance — the MQTT bridge over v3 seams: MQTT -> LoRa -> MQTT, both directions.

The decomposition of the student MQTT-over-LoRa bridge onto the v3 architecture: ingest
is an MQTT_DataSource feeding whoever holds the source role, egress is the MQTT_DataSink,
bidirectionality is role reversal (a GRANT-delegated downlink pull), and topic
preservation is the file-name envelope. Brokers are injected umqtt-shaped fakes, so the
suite stays broker-free; the LoRa link is the loopback pair.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.DataSinks.DataSink import DataSink
from AlLoRa.DataSources.MQTT_DataSource import MQTT_DataSource
from AlLoRa.DataSources.mqtt_naming import decode_name

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
SESSION_ID = 42
HUB_OWN_SID = 7


class Fake_client:
    """Umqtt-shaped fake broker client (see test_mqtt_datasource): subscribe/callback/
    check_msg for the datasource side, publish for the sink side."""

    def __init__(self):
        self.cb = None
        self.subscribed = []
        self.pending = []
        self.published = []

    def set_callback(self, cb):
        self.cb = cb

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def check_msg(self):
        if self.pending:
            topic, payload = self.pending.pop(0)
            self.cb(topic, payload)

    def publish(self, topic, payload):
        self.published.append((topic, bytes(payload)))


class Capture_sink(DataSink):
    def __init__(self):
        self.received = []

    def consume(self, file, reception=None):
        self.received.append((file.get_name(), bytes(file.get_content()), reception))
        file.discard()


def _write_config(path, result_path, session_id):
    config = {
        "name": "mb", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": session_id,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_pair(tmp_path, edge_kwargs=None, hub_kwargs=None):
    edge_config = str(tmp_path / "edge.json")
    hub_config = str(tmp_path / "hub.json")
    _write_config(edge_config, str(tmp_path / "edge_results"), SESSION_ID)
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge = Edge(edge_conn, config_file=edge_config, **(edge_kwargs or {}))
    hub = Hub(hub_conn, config_file=hub_config, **(hub_kwargs or {}))
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC,
                                active=True, session_id=SESSION_ID)
    return edge, hub, endpoint


def test_edge_serves_a_broker_message_to_the_hub(tmp_path):
    # Uplink half: a PUBLISH on the edge-side broker becomes the next file the Edge
    # serves; it lands in the Hub's sink envelope-named, payload byte-exact. The Edge
    # prepares its datasource itself (first serve round), so the deployment loop is
    # just Edge(datasource=...).serve().
    edge_client = Fake_client()
    datasource = MQTT_DataSource(topics=("sensors/#",),
                                 client=edge_client)
    hub_sink = Capture_sink()
    edge, hub, endpoint = _make_pair(
        tmp_path,
        edge_kwargs={"datasource": datasource},
        hub_kwargs={"data_sink": hub_sink},
    )
    edge_client.pending.append((b"sensors/greenhouse/temp", b"21.5"))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 10}, daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=20, save_file=True, one_file=True)
    server.join(timeout=15)
    assert not server.is_alive(), "edge serve loop did not come home"

    assert edge_client.subscribed == ["sensors/#"], "edge did not prepare its datasource"
    assert len(hub_sink.received) == 1, "the broker message never reached the hub sink"
    name, content, reception = hub_sink.received[0]
    topic, artifact_id, ts = decode_name(name)
    assert topic == "sensors/greenhouse/temp"
    assert content == b"21.5"
    assert not datasource.has_pending(), "the served file was not taken off the queue"


def test_hub_delegates_a_broker_message_down_to_the_edge(tmp_path):
    # Downlink half (finding 14's seam): the Hub's per-Edge downlink is a DataSource,
    # so a PUBLISH on the hub-side broker reaches the Edge's sink via the normal
    # GRANT-delegated pull, topic preserved, and control returns to the Hub.
    hub_client = Fake_client()
    downlink = MQTT_DataSource(topics=("downlink/greenhouse/#",),
                               client=hub_client)
    edge_sink = Capture_sink()
    edge, hub, endpoint = _make_pair(
        tmp_path,
        edge_kwargs={"data_sink": edge_sink},
        hub_kwargs={"reclaim_timeout": 3},
    )
    hub.set_downlink_source(endpoint, downlink)
    assert hub_client.subscribed == ["downlink/greenhouse/#"], \
        "registering the source must bring it up (connect + subscribe happen at setup)"
    hub_client.pending.append((b"downlink/greenhouse/valve", b"OPEN"))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 16}, daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=12, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive(), "edge serve loop did not come home"

    assert len(edge_sink.received) == 1, "the downlink never reached the edge sink"
    name, content, _ = edge_sink.received[0]
    assert decode_name(name)[0] == "downlink/greenhouse/valve"
    assert content == b"OPEN"
    assert not hub.downlink_pending(endpoint)
    assert hub.current_role == "collector" and edge.current_role == "source"


def test_bidirectional_mqtt_round_trip_with_loop_guards(tmp_path):
    # The whole bridge at once — the student bridge's deployment shape on v3 seams. Each
    # node works ONE broker for both roles (datasource subscribed to "#", sink
    # republishing to the same broker), which is exactly the setup that ping-pongs
    # forever without loop prevention: every republish echoes back into the local
    # subscription. The guards must keep each message to exactly one crossing while
    # both directions stay topic-preserving and byte-exact.
    from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink
    from AlLoRa.DataSources.Loop_guard import Loop_guard

    edge_client, hub_client = Fake_client(), Fake_client()
    edge_guard, hub_guard = Loop_guard(), Loop_guard()
    edge, hub, endpoint = _make_pair(
        tmp_path,
        edge_kwargs={
            "datasource": MQTT_DataSource(topics=("#",),
                                          client=edge_client, loop_guard=edge_guard),
            "data_sink": MQTT_DataSink(client=edge_client, loop_guard=edge_guard),
        },
        hub_kwargs={
            "data_sink": MQTT_DataSink(client=hub_client, loop_guard=hub_guard),
            "reclaim_timeout": 3,
        },
    )
    hub_downlink = MQTT_DataSource(topics=("#",),
                                   client=hub_client, loop_guard=hub_guard)
    hub.set_downlink_source(endpoint, hub_downlink)

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20}, daemon=True)
    server.start()

    # Leg 1 — uplink: an edge-side reading crosses and republishes topic-intact.
    edge_client.pending.append((b"sensors/greenhouse/temp", b"21.5"))
    hub.listen_to_endpoint(endpoint, listening_time=20, save_file=True, one_file=True)
    assert hub_client.published == [("sensors/greenhouse/temp", b"21.5")]

    # A real broker echoes the sink's publish back into the co-located "#"
    # subscription. Feed that echo ahead of the genuine downlink command: the guard
    # must drop it at the first delegation boundary, then deliver the command.
    hub_client.pending.append(hub_client.published[0])
    hub_client.pending.append((b"downlink/greenhouse/valve", b"OPEN"))

    hub.listen_to_endpoint(endpoint, listening_time=10, save_file=True)
    server.join(timeout=15)
    assert not server.is_alive(), "edge serve loop did not come home"

    # Downlink delivered exactly once, topic preserved, byte-exact — the echo never
    # became a second downlink (the edge-side guard is covered at the unit level).
    assert edge_client.published == [("downlink/greenhouse/valve", b"OPEN")]
    assert hub_client.published == [("sensors/greenhouse/temp", b"21.5")]
    assert not hub_downlink.has_pending()

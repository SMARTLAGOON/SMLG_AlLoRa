"""End to end: a signed CUSTOM command crosses the link and is handed over after the final OK.

CUSTOM carries signed bytes for whatever runs beside AlLoRa: a camera board on an ESP32's serial
wire, or a program on the same Pi. The library verifies them and hands them over, and never
reads them. What this file pins down is the path a deployment's own actuator sits on: the Hub
queues the artifact, the Edge pulls it, the gate verifies it and forwards it only because the
actuator declared CUSTOM, and the hand-off waits until the transfer's final OK is on the air.

The hand-off waiting is the part a slow attached device would break. If the Edge handed over
while still pulling, the acknowledgement would wait on the device, and the Hub would send the
file again.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Control.Control_Actuator import Control_Actuator
from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_types import CUSTOM
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Packet_v3 import Packet_v3
from test_control_root_sink import CONTROL_ROOT_PRIV, TARGET_DEVICE_ID

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
SESSION_ID = 42
HUB_OWN_SID = 7


class _Handover_actuator(Control_Actuator):
    """A deployment's actuator in miniature: it queues the hand-off, and records when it ran
    against the frames the Edge had put on the air by then."""

    handles = (CUSTOM,)

    def __init__(self, node, events):
        self.node = node
        self.events = events

    def apply(self, control_type, payload):
        self.events.append(("applied", bytes(payload)))
        events = self.events
        self.node.queue_control_action(lambda: events.append(("handed over", bytes(payload))))


def _write_config(path, session_id):
    config = {
        "name": "custom", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": session_id,
        "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def test_a_signed_custom_is_handed_over_once_after_the_final_ok(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # the gate keeps its replay mark in the working directory
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    _write_config(str(tmp_path / "edge.json"), SESSION_ID)
    _write_config(str(tmp_path / "hub.json"), HUB_OWN_SID)
    edge = Edge(edge_conn, config_file=str(tmp_path / "edge.json"))
    hub = Hub(hub_conn, config_file=str(tmp_path / "hub.json"), reclaim_timeout=3)

    events = []
    transmit = edge_conn.transmit

    def logged_transmit(wire):
        events.append(("sent", edge_conn.codec.deframe(wire).get_command()))
        return transmit(wire)

    edge_conn.transmit = logged_transmit
    root = Control_Root(CONTROL_ROOT_PRIV)
    edge.data_sink = Control_Root_DataSink(control_root=root.public_key(),
                                           device_id=TARGET_DEVICE_ID,
                                           actuator=_Handover_actuator(edge, events))

    # Minted the way a backend does it: the deployment's own bytes, signed for one Edge.
    artifact = root.mint(CUSTOM, TARGET_DEVICE_ID, 1, b"take a photo")
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                session_id=SESSION_ID, device_id=TARGET_DEVICE_ID.hex())
    hub.queue_downlink(endpoint, AlLoRa_File(name="ctrl.bin", content=bytearray(artifact)))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    marks = [e for e in events if e[0] != "sent"]
    assert marks == [("applied", b"take a photo"), ("handed over", b"take a photo")], \
        "the payload must arrive untouched, be queued once, and be handed over once"
    applied_at = events.index(marks[0])
    handed_at = events.index(marks[1])
    assert events[handed_at - 1] == ("sent", Packet_v3.OK), \
        "the hand-off must wait until the transfer's final OK is on the air"
    assert any(e == ("sent", Packet_v3.OK) for e in events[applied_at:handed_at]), \
        "the final OK is sent after the gate forwards the command, not before"

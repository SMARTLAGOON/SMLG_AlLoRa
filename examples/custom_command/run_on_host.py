#!/usr/bin/env python3
"""Sends a signed CUSTOM command to two Edges over a simulated radio and shows each hand-off.

One Edge stands for an ESP32 with a camera board on its serial wire, the other for a Pi with a
camera program beside AlLoRa. Usage: run_on_host.py [work_dir], default a new temporary folder.
"""
import json
import os
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _HERE)

from AlLoRa.Connectors.Loopback_connector import Loopback_connector   # noqa: E402
from AlLoRa.Control.Control_Root import Control_Root                  # noqa: E402
from AlLoRa.Control.control_types import CUSTOM                       # noqa: E402
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink  # noqa: E402
from AlLoRa.Digital_Endpoint import Digital_Endpoint                  # noqa: E402
from AlLoRa.File import AlLoRa_File                                   # noqa: E402
from AlLoRa.Nodes.Edge import Edge                                    # noqa: E402
from AlLoRa.Nodes.Hub import Hub                                      # noqa: E402
from program_beside_actuator import Program_Beside_Actuator          # noqa: E402
from serial_device_actuator import Serial_Device_Actuator            # noqa: E402

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


class Printing_UART:
    """Stands in for machine.UART: shows what the camera board would receive."""

    def write(self, frame):
        length = int.from_bytes(frame[:2], "big")
        print("serial device got: {}".format(frame[2:2 + length].decode()))


def write_config(path, name, session_id):
    config = {
        "name": name, "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": session_id,
        "debug": False, "result_path": os.path.join(os.path.dirname(path), name + "_results"),
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def send_custom(work_dir, name, root, device_id, counter, payload, make_actuator):
    """One Hub, one Edge, one signed CUSTOM command, delivered over the simulated link."""
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge_config = os.path.join(work_dir, name + "_edge.json")
    hub_config = os.path.join(work_dir, name + "_hub.json")
    write_config(edge_config, name + "_edge", 42)
    write_config(hub_config, name + "_hub", 7)
    edge = Edge(edge_conn, config_file=edge_config)
    hub = Hub(hub_conn, config_file=hub_config, reclaim_timeout=3)

    # The Edge's downlink goes through the verify gate. Only what the control root signed for
    # this device reaches the actuator, and only the types the actuator lists in `handles`.
    edge.data_sink = Control_Root_DataSink(
        control_root=root.public_key(), device_id=device_id,
        actuator=make_actuator(edge),
        counter_file=os.path.join(work_dir, name + "_control.counter"))

    # In a deployment the backend mints this and the Hub only carries it.
    artifact = root.mint(CUSTOM, device_id, counter, payload)
    endpoint = Digital_Endpoint(name=name, mac_address=EDGE_MAC, active=True,
                                session_id=42, device_id=device_id.hex())
    hub.queue_downlink(endpoint, AlLoRa_File(name="ctrl.bin", content=bytearray(artifact)))

    # One visit is enough on a link that loses nothing; the Edge then waits out its timeout.
    server = threading.Thread(target=edge.serve, kwargs={"timeout": 4}, daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=3, save_file=True)
    server.join(timeout=8)


def main():
    work_dir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="allora_custom_")
    os.makedirs(os.path.join(work_dir, "camera"), exist_ok=True)
    root = Control_Root.generate(os.urandom)
    # On real nodes this is each Edge's device_id, the hash of its identity key.
    esp32_id = bytes(range(32))
    pi_id = bytes(range(32, 64))

    send_custom(work_dir, "esp32", root, esp32_id, 1, b"take a photo",
                lambda edge: Serial_Device_Actuator(edge, Printing_UART()))

    model_path = os.path.join(work_dir, "camera", "model.bin")

    def restart_camera_program():
        with open(model_path, "rb") as f:
            print("program restarted, it reads: {}".format(f.read().decode()))

    send_custom(work_dir, "pi", root, pi_id, 2, b"new detection model",
                lambda edge: Program_Beside_Actuator(edge, model_path, restart_camera_program))


if __name__ == "__main__":
    main()

"""Acceptance — a v3 *open-mode* transfer over the loopback seam.

The v2 loopback test (test_loopback_transfer.py) proved the transport seam is clean.
This proves the **v3 typed wire flows end-to-end through the real engine**: a Source
serves a multi-chunk file to a Collector entirely in v3 frames (version nibble, typed
kinds, session-id addressing, typed METADATA, binary CHUNK index, 24-bit integrity) —
open mode, zero crypto. The engine, File and Digital_Endpoint are shared with v2; only
the codec and a handful of version-aware seams differ.

Open mode uses a **statically-configured session id** (like the MAC is configured today);
dynamic sid assignment lands with the secure handshake (increment 2). First-contact /
beacon negotiation is the next sub-step — here both ends already know they're v3.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "loopback-v3",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        # --- v3 open mode ---
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": SESSION_ID,
        "debug": False,
        "result_path": result_path,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _run_transfer(tmp_path, source_conn, collector_conn, payload, filename):
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source = Edge(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename,
                                content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))

    collector = Hub(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve-v3", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30,
                                 save_file=True, one_file=True)
    server.join(timeout=15)

    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"
    return tmp_path / "Results" / SOURCE_MAC / filename


def test_v3_open_source_to_collector_transfer(tmp_path):
    payload = bytes(i % 256 for i in range(1000))  # 5 chunks at 243
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    received = _run_transfer(tmp_path, source_conn, collector_conn, payload, "v3.bin")

    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "v3 open-mode transfer did not reassemble"


def test_v3_open_transfer_recovers_from_lost_replies(tmp_path):
    payload = bytes(i % 256 for i in range(1000))
    source_conn, collector_conn = Loopback_connector.create_pair(
        SOURCE_MAC, COLLECTOR_MAC, loss_a_to_b=0.3, seed=1234)

    received = _run_transfer(tmp_path, source_conn, collector_conn, payload, "v3_lossy.bin")

    assert source_conn.dropped > 0, "loss injection was inert"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "lossy v3 transfer did not reassemble"

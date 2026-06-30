"""Acceptance test — the v3 transport seam.

A `Source` serves a multi-chunk file to a `Collector` (the `Requester` preset)
over an in-memory `Loopback_connector`, on CPython, with no radios.

This is the first v3 test (ADR 0001 §2, handoff "the one next step"). It proves the
transport seam is clean: the protocol engine depends only on the narrow `Connector`
interface, so substituting a fake connector drives an end-to-end transfer in CI.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import CTP_File

# Source = responder (lives with the data), Collector = initiator (drives the transfer).
SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _write_config(path, result_path):
    """A LoRa.json shared by both nodes. RF config matches the Digital_Endpoint
    defaults (SF7/868/125/CR1/14 dBm) so the Collector doesn't try to retune."""
    config = {
        "name": "loopback",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "debug": False,
        "result_path": result_path,
        "connector": {
            "sf": 7,
            "freq": 868,
            "bandwidth": 125,
            "coding_rate": 1,
            "tx_power": 14,
            "min_timeout": 0.5,
            "max_timeout": 6,
            "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def test_source_to_collector_file_transfer_over_loopback(tmp_path):
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    # 1000 bytes spans 5 chunks at chunk_size=243 (243*4 + 28), so the transfer
    # exercises metadata, multi-chunk sequencing and reassembly — not a one-shot.
    payload = bytes(i % 256 for i in range(1000))
    filename = "payload.bin"

    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    source = Source(source_conn, config_file=config_file)
    source.set_file(CTP_File(name=filename,
                             content=bytearray(payload),
                             chunk_size=source.get_chunk_size()))

    collector = Requester(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC, active=True)

    errors = []

    def serve():
        try:
            source.send_file(timeout=20000)  # ms — safety net so the thread can't hang
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30,
                                 save_file=True, one_file=True)

    server.join(timeout=10)

    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"

    received = tmp_path / "Results" / SOURCE_MAC / filename
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "reassembled file does not match the original"

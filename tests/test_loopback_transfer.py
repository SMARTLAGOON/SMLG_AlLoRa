"""Acceptance tests — the v3 transport seam.

A `Source` serves a multi-chunk file to a `Collector` (the `Requester` preset)
over an in-memory `Loopback_connector`, on CPython, with no radios.

This is the first v3 test — it proves the
transport seam is clean: the protocol engine depends only on the narrow `Connector`
interface, so substituting a fake connector drives an end-to-end transfer in CI — and,
with the connector's loss injection, exercises the stop-and-wait retransmission path.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File

# Source = responder (lives with the data), Collector = initiator (drives the transfer).
SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _write_config(path, result_path):
    """A LoRa.json shared by both nodes. RF config matches the Digital_Endpoint
    defaults (SF7/868/125/CR1/14 dBm) so the Collector doesn't try to retune.
    A small timeout_delta keeps per-timeout waits short so the lossy test stays fast."""
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
            "timeout_delta": 0.1,
            "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _run_transfer(tmp_path, source_conn, collector_conn, payload, filename):
    """Wire a Source (responder, background thread) to a Collector (initiator, here),
    run one file transfer over the given connector pair, and return the saved path."""
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename,
                                content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))

    collector = Requester(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC, active=True)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)  # seconds — safety net so the thread can't hang
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30,
                                 save_file=True, one_file=True)

    server.join(timeout=15)

    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"
    return tmp_path / "Results" / SOURCE_MAC / filename


def test_source_to_collector_file_transfer_over_loopback(tmp_path):
    # 1000 bytes spans 5 chunks at chunk_size=243 (243*4 + 28), so the transfer
    # exercises metadata, multi-chunk sequencing and reassembly — not a one-shot.
    payload = bytes(i % 256 for i in range(1000))
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    received = _run_transfer(tmp_path, source_conn, collector_conn, payload, "payload.bin")

    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "reassembled file does not match the original"


def test_transfer_recovers_from_lost_replies(tmp_path):
    payload = bytes(i % 256 for i in range(1000))

    # Drop 30% of the Source->Collector replies (METADATA / DATA / OK). Requests and the
    # fire-and-forget final OK (Collector->Source) are never dropped, so the Source still
    # terminates cleanly; the Collector's stop-and-wait must retransmit the gaps. Seeded
    # so the drop sequence is deterministic (only the Source thread drives that end's send).
    source_conn, collector_conn = Loopback_connector.create_pair(
        SOURCE_MAC, COLLECTOR_MAC, loss_a_to_b=0.3, seed=1234)

    received = _run_transfer(tmp_path, source_conn, collector_conn, payload, "lossy.bin")

    assert source_conn.dropped > 0, "no replies were dropped — loss injection was inert"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "lossy transfer did not reassemble correctly"

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
from AlLoRa.Packet_v3 import Packet_v3

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "loopback-v3",
        "chunk_size": 243,
        "mesh_mode": False,
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


class _ResettingEdge(Edge):
    """An Edge that swaps the file it is serving partway through a transfer.

    This is what a board that reset looks like from the Hub: it comes back holding
    whatever its DataSource hands it next and answers the next chunk request from
    that, because a chunk request carries an index and nothing that identifies the
    file. Swapping on the request itself keeps the test deterministic, where waiting
    on a timeout would not be.
    """

    def arm_swap(self, at_index, replacement):
        self._swap_at = at_index
        self._replacement = replacement
        self._swapped = False

    def response(self, packet):
        if (not self._swapped and packet.get_command() == Packet_v3.CHUNK
                and packet.get_chunk_index() == self._swap_at):
            self._swapped = True
            self.set_file(self._replacement)
        return super().response(packet)


def test_a_peer_that_changes_file_mid_transfer_cannot_splice(tmp_path):
    """The 2026-08-09 hardware defect, reproduced over the loopback seam.

    A Hub mid-transfer of a 500-byte file had the serving board reset under it. It
    saved 600 bytes under that file's name and reported success, with contents spliced
    from two different files, because completeness was counted (no index missing) and
    never measured against the byte total METADATA already carries.
    """
    original = bytes(i % 256 for i in range(500))            # 3 chunks at 243: 243/243/14
    replacement = bytes((i * 7) % 256 for i in range(600))   # 3 chunks at 243: 243/243/114

    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    source = _ResettingEdge(source_conn, config_file=config_file)
    chunk_size = source.get_chunk_size()
    source.set_file(AlLoRa_File(name="foxtrot.bin", content=bytearray(original),
                                chunk_size=chunk_size))
    # Swap on the request for the final chunk: the index the peer answers is valid in
    # both files, and only the length gives the substitution away.
    source.arm_swap(2, AlLoRa_File(name="hello.bin", content=bytearray(replacement),
                                   chunk_size=chunk_size))

    collector = Hub(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="resetting-source", daemon=True)
    server.start()
    collector.listen_to_endpoint(endpoint, listening_time=30,
                                 save_file=True, one_file=True)
    server.join(timeout=15)
    assert not errors, "source thread raised: {}".format(errors)

    saved = tmp_path / "Results" / SOURCE_MAC
    spliced = saved / "foxtrot.bin"
    assert not spliced.exists(), (
        "the interrupted file was saved anyway, at {} bytes".format(
            spliced.stat().st_size if spliced.exists() else 0))

    # Refusing the chunk is not enough on its own: the Hub has to re-open the transfer
    # from METADATA, which is the only way it learns what the peer now holds.
    rescued = saved / "hello.bin"
    assert rescued.exists(), "the Hub never re-pulled the file the peer actually had"
    assert rescued.read_bytes() == replacement


def test_a_peer_that_reboots_onto_a_same_length_file_cannot_splice(tmp_path):
    """The deployment case the length check alone is blind to.

    A node writing fixed-format sensor records produces files that are frequently
    identical in length, so the byte total each chunk is measured against is the same in
    both files and every substituted chunk fits. Nothing about the sizes gives it away.

    What does is that a source coming back from a reset no longer remembers announcing
    the file it is being asked for, so it re-announces instead of serving. The Hub then
    has to hear that answer for what it is: not a lost frame, but the peer saying it is
    serving something else now.
    """
    original = bytes(i % 256 for i in range(500))            # 3 chunks at 243
    replacement = bytes((i * 7) % 256 for i in range(500))   # same length, other bytes

    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    source = _ResettingEdge(source_conn, config_file=config_file)
    chunk_size = source.get_chunk_size()
    source.set_file(AlLoRa_File(name="foxtrot.bin", content=bytearray(original),
                                chunk_size=chunk_size))
    # set_file is what a reset leaves behind: a file with its delivery state cleared, so
    # nothing on it records an announcement. Armed on the request for the final chunk.
    source.arm_swap(2, AlLoRa_File(name="hello.bin", content=bytearray(replacement),
                                   chunk_size=chunk_size))

    collector = Hub(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=20)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="rebooting-source", daemon=True)
    server.start()
    collector.listen_to_endpoint(endpoint, listening_time=20,
                                 save_file=True, one_file=True)
    server.join(timeout=15)
    assert not errors, "source thread raised: {}".format(errors)

    saved = tmp_path / "Results" / SOURCE_MAC
    spliced = saved / "foxtrot.bin"
    assert not spliced.exists(), (
        "the interrupted file was saved anyway, at {} bytes".format(
            spliced.stat().st_size if spliced.exists() else 0))

    rescued = saved / "hello.bin"
    assert rescued.exists(), "the Hub never re-pulled the file the peer actually had"
    assert rescued.read_bytes() == replacement

    # Re-opening the transfer replaces the reassembly, which on a constrained board means
    # an open writer and its temp file are dropped. Neither may be left behind: the
    # descriptor table is tiny and the flash is smaller.
    abandoned = saved / "Temp" / "foxtrot.bin.tmp"
    assert not abandoned.exists(), "the abandoned reassembly leaked its temp file"

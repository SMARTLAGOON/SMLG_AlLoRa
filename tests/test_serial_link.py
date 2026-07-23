"""Serial_link — the UART byte-mover under a serial tunnel, proven on CPython.

The link's job is pure framing: delimit opaque request/reply frames on a byte stream, survive
noise, honour a timeout. We test that directly with an in-memory full-duplex "UART" (two
byte pipes), and then run a *whole* v3 file transfer through a Serial_link pair — the same
acceptance test_tunnel_transfer runs over Loopback_link, but over the real sentinel framing.
The remaining unknowns (a physical UART's timing, wiring) are what the hardware pass covers.
"""
import json
import threading

from AlLoRa.Links.Serial_link import Serial_link
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


class _Pipe:
    """A thread-safe in-memory byte stream: one side puts, the other gets."""

    def __init__(self):
        self._buf = bytearray()
        self._lock = threading.Lock()

    def put(self, data):
        with self._lock:
            self._buf.extend(data)

    def get(self, n):
        with self._lock:
            if not self._buf:
                return b""
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out

    def __len__(self):
        with self._lock:
            return len(self._buf)


class _PipePort:
    """One end of an in-memory full-duplex UART: writes to `tx`, reads from `rx`, and mirrors
    pyserial's `in_waiting` + `read(n)` so Serial_link drives it exactly like a real port."""

    def __init__(self, tx, rx):
        self._tx = tx
        self._rx = rx

    def write(self, data):
        self._tx.put(bytes(data))
        return len(data)

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        return self._rx.get(n)


def _serial_pair(**kwargs):
    down, up = _Pipe(), _Pipe()                    # down: client->bridge; up: bridge->client
    client = Serial_link(_PipePort(tx=down, rx=up), **kwargs)
    bridge = Serial_link(_PipePort(tx=up, rx=down), **kwargs)
    return client, bridge


# --- framing unit tests ----------------------------------------------------------------------

def test_bridge_reads_sentinel_framed_requests_in_order():
    client, bridge = _serial_pair()
    # A single blob carrying two frames + a partial third: the reader must split on the
    # sentinel, keep order, and leave the partial buffered.
    client._port.write(b"AAA" + Serial_link.SENTINEL + b"BBBB" + Serial_link.SENTINEL + b"CC")
    assert bridge.read_request(timeout=1) == b"AAA"
    assert bridge.read_request(timeout=1) == b"BBBB"
    assert bridge.read_request(timeout=0.2) is None      # third frame never terminated


def test_read_frame_times_out_to_none():
    _client, bridge = _serial_pair()
    assert bridge.read_request(timeout=0.3) is None


def test_rpc_round_trips_request_and_reply():
    client, bridge = _serial_pair()

    def serve():
        req = bridge.read_request(timeout=2)
        assert req == b'{"v":"tx"}'
        bridge.write_reply(b'{"ok":true}')

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    reply = client.rpc(b'{"v":"tx"}', timeout=2)
    t.join(timeout=2)
    assert reply == b'{"ok":true}'


def test_text_safe_filter_drops_line_noise_around_a_frame():
    client, bridge = _serial_pair(text_safe=True)
    # Non-printable bytes (a brown-out burst) bracket a valid frame; text_safe drops them so
    # the frame still parses instead of forcing a retransmit.
    client._port.write(bytes([0x00, 0xFF, 0x80]) + b"HELLO" + Serial_link.SENTINEL)
    assert bridge.read_request(timeout=1) == b"HELLO"


def test_rpc_flushes_a_stale_reply_before_the_next_request():
    client, bridge = _serial_pair()
    # A late reply to a request we already gave up on is sitting in the buffer; the next rpc
    # must discard it, not return it as the answer to the new request.
    bridge.write_reply(b"STALE")
    while len(client._port._rx) == 0:
        pass

    def serve():
        req = bridge.read_request(timeout=2)
        assert req == b"FRESH"
        bridge.write_reply(b"ANSWER")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    reply = client.rpc(b"FRESH", timeout=2)
    t.join(timeout=2)
    assert reply == b"ANSWER"


# --- full tunneled transfer over the real framing --------------------------------------------

def _write_config(path, result_path):
    config = {
        "name": "tunnel-serial", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return config


def test_full_v3_transfer_over_serial_link(tmp_path):
    payload = bytes(i % 256 for i in range(1000))          # 5 chunks at 243
    source_conn, bridge_radio = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    config = _write_config(config_file, result_path)
    bridge_radio.config(config["connector"])

    client_link, bridge_link = _serial_pair()
    iface = Tunnel_interface(link=bridge_link)
    iface.setup(bridge_radio, debug=False, config={})
    stop = threading.Event()
    pump = threading.Thread(target=lambda: iface.serve(should_stop=stop.is_set), daemon=True)
    pump.start()

    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name="serial.bin", content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))
    collector = Requester(Tunnel_connector(link=client_link), config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC, active=True,
                                session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve-serial", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
    server.join(timeout=15)
    stop.set()
    pump.join(timeout=2)

    assert not errors, "source thread raised: {}".format(errors)
    received = tmp_path / "Results" / SOURCE_MAC / "serial.bin"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "tunneled v3 transfer over Serial_link did not reassemble"

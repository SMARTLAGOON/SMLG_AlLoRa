"""Acceptance — a full v3 transfer with the Collector *tunneled* across a split Connector.

test_v3_open_transfer proved a v3 transfer over a single local radio. This proves the same
transfer when the Collector's logic runs on one device and its radio on another: the Requester
drives a Tunnel_connector, whose transport verbs cross a Loopback_link to a Tunnel_interface
that runs them on the real (loopback) radio. The Source is a standalone board on the far side.

The point the split closes: the bridge is dumb + keyless — it ferries opaque wire and matches
replies on a cleartext prefix at the radio, so the *engine* (File, Digital_Endpoint, the
request/respond verbs, the codec) is entirely on the logic-holder and works over the tunnel
byte-for-byte as it does locally. This is why a single bridge serves v2 / v3-open / v3-secure
alike (the old tunnels re-parsed the frame, so they only spoke v2). The lossy variant proves
the exchange-routed match loop + retransmission recover across the link, not just a clean one.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Links.Loopback_link import Loopback_link
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "tunnel-v3", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return config


def _run_tunneled_transfer(tmp_path, source_conn, bridge_radio, payload, filename):
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    config = _write_config(config_file, result_path)

    # The bridge (Adapter) side: a Tunnel_interface wrapping the real radio, pumped in a thread.
    bridge_radio.config(config["connector"])          # its RF, so get_rf_config/timeouts are set
    client_link, bridge_link = Loopback_link.create_pair()
    iface = Tunnel_interface(link=bridge_link)
    iface.setup(bridge_radio, debug=False, config={})
    stop = threading.Event()
    pump = threading.Thread(target=lambda: iface.serve(should_stop=stop.is_set), daemon=True)
    pump.start()

    # The Source is a standalone node; the Collector drives its radio over the tunnel.
    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename, content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))
    collector = Requester(Tunnel_connector(link=client_link), config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve-tunnel", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
    server.join(timeout=15)
    stop.set()
    pump.join(timeout=2)

    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"
    return tmp_path / "Results" / SOURCE_MAC / filename


def test_tunneled_v3_open_transfer(tmp_path):
    payload = bytes(i % 256 for i in range(1000))          # 5 chunks at 243
    source_conn, bridge_radio = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    received = _run_tunneled_transfer(tmp_path, source_conn, bridge_radio, payload, "tun.bin")

    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "tunneled v3 transfer did not reassemble"


def test_tunneled_v3_transfer_recovers_from_lost_replies(tmp_path):
    payload = bytes(i % 256 for i in range(1000))
    # Drop 30% of the Source's replies before they reach the bridge radio: the exchange loop
    # at the radio times out, the Collector retransmits the whole round over the tunnel.
    source_conn, bridge_radio = Loopback_connector.create_pair(
        SOURCE_MAC, COLLECTOR_MAC, loss_a_to_b=0.3, seed=1234)

    received = _run_tunneled_transfer(tmp_path, source_conn, bridge_radio, payload, "tun_lossy.bin")

    assert source_conn.dropped > 0, "loss injection was inert"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "lossy tunneled transfer did not reassemble"

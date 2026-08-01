"""WiFi_link — the HTTP byte-mover under a WiFi tunnel, proven on CPython over localhost.

The link's job is one HTTP round trip per verb: POST a request body, read the reply body. We
test that over real loopback sockets (the same BSD API the ESP32's usocket serves), and then
run a whole v3 file transfer through a WiFi_link pair — the acceptance test_tunnel_transfer
runs over Loopback_link, here over real sockets. Wiring/network bring-up is the hardware pass.
"""
import json
import socket
import threading

from AlLoRa.Links.WiFi_link import WiFi_link
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _link_pair():
    bridge = WiFi_link.bridge(host="127.0.0.1", port=0, socket_module=socket)
    client = WiFi_link.client(host="127.0.0.1", port=bridge.bound_port(),
                              timeout=5, socket_module=socket)
    return client, bridge


def test_rpc_round_trips_the_body_over_localhost():
    client, bridge = _link_pair()
    frame = b'{"v":"xc","wire":"deadbeef","w":6,"m":"2a"}'

    def serve():
        req = bridge.read_request(timeout=3)
        assert req == frame
        bridge.write_reply(b'{"wire":"cafe","td":11,"st":"matched"}')

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    reply = client.rpc(frame, timeout=3)
    t.join(timeout=3)
    assert reply == b'{"wire":"cafe","td":11,"st":"matched"}'
    bridge.close()


def test_read_request_returns_none_on_accept_timeout():
    _client, bridge = _link_pair()
    assert bridge.read_request(timeout=0.3) is None
    bridge.close()


def test_full_v3_transfer_over_wifi_link(tmp_path):
    payload = bytes(i % 256 for i in range(1000))          # 5 chunks at 243
    source_conn, bridge_radio = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    config = {
        "name": "tunnel-wifi", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(config_file, "w") as f:
        json.dump(config, f)
    bridge_radio.config(config["connector"])

    client_link, bridge_link = _link_pair()
    bridge = Adapter(bridge_radio, link=bridge_link)
    stop = threading.Event()
    pump = threading.Thread(target=lambda: bridge.serve(should_stop=stop.is_set), daemon=True)
    pump.start()

    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name="wifi.bin", content=bytearray(payload),
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

    server = threading.Thread(target=serve, name="source-serve-wifi", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
    server.join(timeout=15)
    stop.set()
    pump.join(timeout=2)
    bridge_link.close()

    assert not errors, "source thread raised: {}".format(errors)
    received = tmp_path / "Results" / SOURCE_MAC / "wifi.bin"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "tunneled v3 transfer over WiFi_link did not reassemble"

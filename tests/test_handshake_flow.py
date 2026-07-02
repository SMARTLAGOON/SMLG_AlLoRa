"""Acceptance — the ECDH handshake run over the live request/respond flow.

Where test_handshake.py drove the handshake steps by hand, this runs them through the real
engine: the Collector drives two rounds with `send_request` over open MAC CTRL frames, the
Source answers inside `respond`, and both ends come out holding the matching per-direction
session — keyed by the sid the Collector assigned — without any pre-shared secret. A frame
the Source then seals opens at the Collector, proving the session works. (Wiring this into
the endpoint state machine so a transfer starts from first contact is the next slice.)
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Packet_v3 import Packet_v3

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 55


def _config(path, result_path):
    config = {
        "name": "hs", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "secure", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def test_handshake_over_the_live_flow_yields_matching_sessions(tmp_path):
    config_file = str(tmp_path / "LoRa.json")
    _config(config_file, str(tmp_path / "Results"))
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    source = Source(source_conn, config_file=config_file)
    collector = Requester(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            rounds = 10   # safety bound; the handshake is two rounds
            while source.session_store.get(SESSION_ID) is None and rounds > 0:
                source.respond(source._handshake_responder)
                rounds -= 1
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-handshake", daemon=True)
    server.start()

    session_r = collector.perform_handshake(endpoint)
    server.join(timeout=15)

    assert not errors, "source raised: {}".format(errors)
    assert session_r is not None, "collector did not complete the handshake"
    session_i = source.session_store.get(SESSION_ID)
    assert session_i is not None, "source did not establish a session"

    # both ends derived the same key over the wire, with agreeing per-direction prefixes
    assert session_i.key == session_r.key
    assert session_i.send_nonce_prefix == session_r.recv_nonce_prefix
    assert session_i.recv_nonce_prefix == session_r.send_nonce_prefix

    # and a frame the Source seals opens at the Collector under those handshake sessions
    aead = source.aead
    p = Packet_v3(addressing="sid")
    p.set_session(SESSION_ID)
    p.set_data(b"post-handshake reading")
    got = Packet_v3(addressing="sid")
    assert got.load_secure(p.get_secure_content(session_i, aead), session_r, aead) is True
    assert got.get_payload() == b"post-handshake reading"

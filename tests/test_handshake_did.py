"""Acceptance + unit — v3 first contact is device_id-addressed.

The two-MAC handshake header gives way to a single device_id[:4] token: the Collector polls
the Source's registered fingerprint, the Source answers under it, no MAC on the wire. And
because both ends derive the same 1-byte sid from that device_id (device_id[0]), the WELCOME
no longer needs to carry the sid — it is dropped except when the Collector reassigns it on a
clash. Address-follows-registration: an endpoint registered by device_id takes this path; one
registered by MAC keeps the legacy MAC-addressed handshake (covered by test_handshake_flow).
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.handshake import (
    initiator_hello, responder_accept, initiator_complete, _PUB_LEN,
)
from AlLoRa.Security.ec_p256 import generate_private_key

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _config(path, result_path):
    config = {
        "name": "hs", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "secure",   # no session_id -> derive from device_id
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


# --- WELCOME sheds the sid byte (both ends derive it) unless reassigned --------------------

def test_welcome_omits_the_sid_when_it_is_identity_derived():
    static_priv = generate_private_key(lambda n: b"\x07" * n)
    state, hello = initiator_hello(lambda n: b"\x08" * n)
    session_r, welcome = responder_accept(static_priv, hello, sid=99, send_sid=False)
    # no sid byte on the wire: welcome is exactly the responder's public key
    assert len(welcome) == _PUB_LEN
    # the initiator falls back to the sid both ends already agree on (device_id[0])
    session_i = initiator_complete(state, welcome, default_sid=99)
    assert session_i.sid == session_r.sid == 99
    assert session_i.key == session_r.key


def test_welcome_carries_the_sid_when_the_collector_reassigns_it():
    static_priv = generate_private_key(lambda n: b"\x07" * n)
    state, hello = initiator_hello(lambda n: b"\x08" * n)
    # a clash forced the Collector off the derived value onto a free sid -> it must be sent
    session_r, welcome = responder_accept(static_priv, hello, sid=200, send_sid=True)
    assert len(welcome) == _PUB_LEN + 1
    # the carried sid wins over whatever the initiator would have derived
    session_i = initiator_complete(state, welcome, default_sid=7)
    assert session_i.sid == session_r.sid == 200


# --- acceptance: the did-addressed handshake over the live request/respond flow ------------

def test_did_registered_handshake_yields_matching_sessions(tmp_path):
    config_file = str(tmp_path / "LoRa.json")
    _config(config_file, str(tmp_path / "Results"))
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    source = Source(source_conn, config_file=config_file)
    collector = Requester(collector_conn, config_file=config_file)

    # register the Source by its device_id (the operator's "same gesture as a MAC")
    endpoint = Digital_Endpoint(name="src", active=True, device_id=source.device_id)
    assert endpoint.session_id == source.device_id[0]       # sid identity-derived on both ends

    errors = []

    def serve():
        try:
            rounds = 10
            while source.session_store.get(source.session_id) is None and rounds > 0:
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
    session_i = source.session_store.get(source.device_id[0])
    assert session_i is not None, "source did not establish a session"

    assert session_i.sid == session_r.sid == source.device_id[0]
    assert session_i.key == session_r.key
    assert session_i.send_nonce_prefix == session_r.recv_nonce_prefix
    assert session_i.recv_nonce_prefix == session_r.send_nonce_prefix

    # a frame the Source seals opens at the Collector under those handshake sessions
    aead = source.aead
    p = Packet_v3(addressing="sid")
    p.set_session(source.device_id[0])
    p.set_data(b"post-handshake reading")
    got = Packet_v3(addressing="sid")
    assert got.load_secure(p.get_secure_content(session_i, aead), session_r, aead) is True
    assert got.get_payload() == b"post-handshake reading"

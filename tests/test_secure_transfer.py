"""Acceptance — a v3 *secure-mode* transfer over the loopback seam.

This is where the secure pieces come together through the real engine: a Source serves a
multi-chunk file to a Collector with every frame — both directions — AEAD-sealed. The
Collector seals its requests under its session, the Source seals its responses under its own,
and the per-direction nonce prefixes keep the two directions from ever sharing a (key, nonce)
even though both start counting at 1. Reassembly must equal the original, and the lossy
variant proves stop-and-wait retransmission still works when each retransmit is a fresh
sealed frame (a new counter, so the receiver's anti-replay window accepts it).

First contact is stubbed here: the two ends' matching per-direction sessions are produced by
running the ECDH handshake up front and dropped into each node's session store. Threading the
handshake into the live Collector-driven flow is the next step; what this proves is the
*secure data path*.
"""
import json
import os
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File
from AlLoRa.Security.handshake import initiator_hello, responder_accept, initiator_complete
from AlLoRa.Security.ec_p256 import generate_private_key

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "loopback-secure", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "secure", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _handshake_sessions(sid):
    """Run the ECDH handshake to get the matching per-direction sessions the two ends would
    derive on first contact. The Source is the handshake initiator; the Collector the
    responder (it assigns the sid)."""
    static_priv = generate_private_key(os.urandom)       # the Collector's long-lived key
    state, hello = initiator_hello(os.urandom)           # Source -> Collector
    session_r, welcome = responder_accept(static_priv, hello, sid)   # Collector (responder)
    session_i = initiator_complete(state, welcome)       # Source (initiator)
    return session_i, session_r


def _run_secure_transfer(tmp_path, source_conn, collector_conn, payload, filename,
                         pre_share=True):
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename, content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))
    collector = Requester(collector_conn, config_file=config_file)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    if pre_share:
        # stub first contact: each end holds its own per-direction session under the sid
        session_i, session_r = _handshake_sessions(SESSION_ID)
        source.session_store.put(session_i)
        collector.session_store.put(session_r)
    # else: the sessions are established live by the handshake inside listen_to_endpoint

    errors = []

    def serve():
        try:
            source.send_file(timeout=30000)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve-secure", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)

    server.join(timeout=15)
    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"
    return tmp_path / "Results" / SOURCE_MAC / filename


def test_secure_source_to_collector_transfer(tmp_path):
    payload = bytes(i % 256 for i in range(1000))   # 5 chunks at chunk_size 243
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    received = _run_secure_transfer(tmp_path, source_conn, collector_conn, payload, "secure.bin")

    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "reassembled secure file does not match"


def test_secure_transfer_recovers_from_lost_replies(tmp_path):
    payload = bytes(i % 256 for i in range(1000))
    source_conn, collector_conn = Loopback_connector.create_pair(
        SOURCE_MAC, COLLECTOR_MAC, loss_a_to_b=0.3, seed=1234)

    received = _run_secure_transfer(tmp_path, source_conn, collector_conn, payload, "secure-lossy.bin")

    assert source_conn.dropped > 0, "no replies were dropped — loss injection was inert"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "lossy secure transfer did not reassemble correctly"


def test_secure_transfer_from_first_contact(tmp_path):
    # No pre-shared session: the ECDH handshake runs live inside listen_to_endpoint, then the
    # transfer proceeds sealed — the whole secure flow, start to finish, over the wire.
    payload = bytes(i % 256 for i in range(1000))
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    received = _run_secure_transfer(tmp_path, source_conn, collector_conn, payload,
                                    "first-contact.bin", pre_share=False)

    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "first-contact secure transfer did not reassemble"


def test_secure_first_contact_survives_dropped_handshake_frames(tmp_path):
    # 30% of Source->Collector frames drop — including HELLO / ACK during the handshake. The
    # handshake's per-round retries (fresh ephemeral on a retried INIT, idempotent re-ACK on a
    # retried WELCOME) must recover it, then stop-and-wait recovers the sealed transfer.
    payload = bytes(i % 256 for i in range(1000))
    source_conn, collector_conn = Loopback_connector.create_pair(
        SOURCE_MAC, COLLECTOR_MAC, loss_a_to_b=0.3, seed=7)

    received = _run_secure_transfer(tmp_path, source_conn, collector_conn, payload,
                                    "first-contact-lossy.bin", pre_share=False)

    assert source_conn.dropped > 0, "no frames were dropped — loss injection was inert"
    assert received.exists(), "collector never saved the file at {}".format(received)
    assert received.read_bytes() == payload, "lossy first-contact transfer did not reassemble"

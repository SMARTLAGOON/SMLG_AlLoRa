"""Unit — the shared one-round verbs on the Node (request / respond).

Both roles run the same round from opposite sides, so request (initiator) and respond
(responder) live on the Node — role reversal is just a node calling the other verb. The
transfer tests exercise both end-to-end; this pins respond's routing directly: a for-me
request goes to the handler, a foreign one does not (it's forwarded / dropped), and an empty
window returns None so the role-specific driver can do its own bookkeeping.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Packet_v3 import Packet_v3

SESSION_ID = 42


def _make_source(tmp_path):
    config = {
        "name": "resp", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": str(tmp_path / "Results"),
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config_file = str(tmp_path / "LoRa.json")
    with open(config_file, "w") as f:
        json.dump(config, f)
    conn, _peer = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    return Source(conn, config_file=config_file), conn


def _request(sid):
    p = Packet_v3(addressing="sid")
    p.set_session(sid)
    p.ask_metadata()
    return p


def test_respond_hands_a_for_me_request_to_the_handler(tmp_path):
    source, conn = _make_source(tmp_path)
    conn.inbox.put(_request(SESSION_ID).get_content())      # a request addressed to us

    seen = []
    got = source.respond(lambda pkt: seen.append(pkt))

    assert got is not None
    assert len(seen) == 1
    assert seen[0].get_session() == SESSION_ID
    assert seen[0].get_command() == Packet_v3.METADATA


def test_respond_does_not_hand_a_foreign_request_to_the_handler(tmp_path):
    source, conn = _make_source(tmp_path)
    conn.inbox.put(_request(SESSION_ID + 1).get_content())   # a different session

    seen = []
    got = source.respond(lambda pkt: seen.append(pkt))

    assert got is not None      # a frame was received...
    assert seen == []           # ...but not for us, so the handler is never called


def test_respond_returns_none_when_nothing_arrives(tmp_path):
    source, _ = _make_source(tmp_path)
    source.connector.adaptive_timeout = 0.3      # keep the empty listen window short

    seen = []
    got = source.respond(lambda pkt: seen.append(pkt))

    assert got is None
    assert seen == []

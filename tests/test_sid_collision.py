"""The 1-byte sid can clash; the Collector resolves it.

Two Sources whose device_id[0] happen to coincide would derive the same sid. The Collector —
which holds every registered Source's device_id — keeps one on its derived value and bumps the
other to a free byte, which it then sends in that session's WELCOME (the one case the initiator
can't reproduce from its own identity). The Source adopts the reassigned sid for its data phase.

Loopback is point-to-point (no broadcast), so a literal 3-node bus isn't modelled here: the
clash detection is a unit check on the resolver, and the over-the-wire half — a Source taking a
Collector-assigned sid it did not derive — is the acceptance below.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint, assign_session_ids
from AlLoRa.Packet_v3 import Packet_v3

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


# --- unit: the resolver keeps one derived sid, bumps the clashing other ---------------------

def test_assign_session_ids_breaks_a_device_id_first_byte_clash():
    ep_a = Digital_Endpoint(name="a", device_id=b"\x42\xaa\xaa\xaa")
    ep_b = Digital_Endpoint(name="b", device_id=b"\x42\xbb\xbb\xbb")   # same first byte -> same sid
    assert ep_a.session_id == ep_b.session_id == 0x42

    assign_session_ids([ep_a, ep_b])

    assert ep_a.session_id == 0x42                 # first keeps its identity-derived sid
    assert ep_b.session_id != ep_a.session_id      # second is bumped off the clash
    # the addresses stay distinct regardless (different device_id[:4] tokens)
    assert ep_a.get_did() != ep_b.get_did()


def test_assign_session_ids_reserves_explicit_overrides_first():
    fixed = Digital_Endpoint(name="fixed", device_id=b"\x10\x00\x00\x01", session_id=0x11)
    derived = Digital_Endpoint(name="derived", device_id=b"\x11\x00\x00\x02")  # derives 0x11 -> clashes
    assign_session_ids([fixed, derived])
    assert fixed.session_id == 0x11                # the override is untouched
    assert derived.session_id != 0x11             # the derived one yields


# --- acceptance: a Source adopts a sid the Collector reassigned over the WELCOME -------------

def _config(path, result_path):
    config = {
        "name": "hs", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "secure",
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def test_source_adopts_a_reassigned_sid_from_the_welcome(tmp_path):
    config_file = str(tmp_path / "LoRa.json")
    _config(config_file, str(tmp_path / "Results"))
    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)

    source = Edge(source_conn, config_file=config_file)
    collector = Hub(collector_conn, config_file=config_file)

    derived = source.device_id[0]
    reassigned = (derived + 1) % 256               # a sid the Source would NOT derive on its own
    endpoint = Digital_Endpoint(name="src", active=True,
                                device_id=source.device_id, session_id=reassigned)

    errors = []

    def serve():
        try:
            rounds = 10
            while source.session_store.get(reassigned) is None and rounds > 0:
                source.respond(source._handshake_responder)
                rounds -= 1
        except Exception as e:  # pragma: no cover
            errors.append(e)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    session_r = collector.perform_handshake(endpoint)
    server.join(timeout=15)

    assert not errors, "source raised: {}".format(errors)
    assert session_r is not None and session_r.sid == reassigned
    # the Source stored the session under the reassigned sid and moved its data-phase sid to it
    session_i = source.session_store.get(reassigned)
    assert session_i is not None
    assert source.session_store.get(derived) is None
    assert source.session_id == reassigned

    # and the reassigned session actually works end to end
    aead = source.aead
    p = Packet_v3(addressing="sid"); p.set_session(reassigned); p.set_data(b"reassigned reading")
    got = Packet_v3(addressing="sid")
    assert got.load_secure(p.get_secure_content(session_i, aead), session_r, aead) is True
    assert got.get_payload() == b"reassigned reading"

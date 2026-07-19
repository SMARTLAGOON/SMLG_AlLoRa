"""Serve-engine behavior at the responder's public seam: `response` (one reply per
request) and `send_file` (the whole-file serve loop). These pin the regressions the
role-reversal review confirmed in the shared engine — behaviors both an Edge serving
its uplink and a Hub serving a delegated downlink rely on.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Packet import Packet
from AlLoRa.Packet_v3 import Packet_v3

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


def _write_config(path, session_id=SESSION_ID):
    config = {
        "name": "engine",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": session_id,
        "debug": False,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_edge(tmp_path):
    config_path = str(tmp_path / "edge.json")
    _write_config(config_path)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=config_path)


def _make_v2_edge(tmp_path):
    # A legacy deployment: no protocol_version in the config (defaults to v2),
    # MAC addressing on the wire.
    config_path = str(tmp_path / "edge_v2.json")
    config = {
        "name": "engine-v2",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "debug": False,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(config_path, "w") as f:
        json.dump(config, f)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=config_path)


def _request(kind_setter, *args):
    packet = Packet_v3(addressing="sid")
    packet.set_session(SESSION_ID)
    kind_setter(packet, *args)
    return packet


def _chunk_request(index):
    return _request(Packet_v3.ask_data, index)


# --- send_file exit accounting ------------------------------------------------

def test_send_file_timeout_after_only_chunk_zero_reports_partial(tmp_path):
    # The peer pulled exactly chunk 0 (a 0-based index — falsy!) and went silent.
    # A timed-out send_file must report "partially sent" (True), not "nothing sent".
    edge = _make_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(500)),
                              chunk_size=edge.get_chunk_size()))

    reply, _ = edge.response(_chunk_request(0))
    assert reply is not None, "chunk 0 was never served — test setup broken"

    # One junk frame makes the first silent respond round return instantly.
    edge.connector.inbox.put(b"\xff")
    assert edge.send_file(timeout=0) is True


# --- idle serving must survive a v2 RF-change request -------------------------

def test_v2_rf_change_while_idle_does_not_crash_serve(tmp_path):
    # Idle serving with no file is the Edge's resting state now; a v2 peer asking
    # for an RF change (with a new chunk size) must get its confirming reply and
    # the change applied — not crash the serve loop on the missing file.
    edge = _make_v2_edge(tmp_path)
    assert edge.file is None

    request = Packet(mesh_mode=False, short_mac=True)
    request.set_source(HUB_MAC)
    request.set_destination(EDGE_MAC)
    request.set_change_rf({"sf": 8, "cks": 100})
    edge.connector.inbox.put(request.get_content())

    edge.serve(timeout=1)      # AttributeError here before the guard

    assert edge.chunk_size == 100, "the RF change was not applied"
    assert not edge.connector.outbox.empty(), "the confirming reply never went out"


# --- radio-adjacent paths must be silent unless debug is on -------------------

def test_quiet_paths_stay_quiet_without_debug(tmp_path, capsys):
    # UART printing costs real time next to the radio loop: with debug off, a
    # granted pull's connector prep and the serve loop's connection wait must not
    # write a byte to stdout.
    from AlLoRa.Digital_Endpoint import Digital_Endpoint

    edge = _make_edge(tmp_path)
    endpoint = Digital_Endpoint(config={
        "name": "hub", "mac_address": HUB_MAC, "active": True,
        "freq": 868, "sf": 7, "bw": 125, "cr": 1, "tx_power": 14,
        "session_id": SESSION_ID,
    })
    capsys.readouterr()    # drop the construction banner

    assert edge.prepare_connector(endpoint) is True

    edge.connector.inbox.put(b"\xff")    # instant round, nothing parseable
    edge.establish_connection(try_for=1)

    assert capsys.readouterr().out == ""


# --- the OK kind is overloaded: connection poll vs the fire-and-forget final-OK

def _served_edge(tmp_path, chunks_served):
    # An Edge mid-uplink: a 3-chunk file with `chunks_served` tail-less requests done.
    edge = _make_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(500)),
                              chunk_size=edge.get_chunk_size()))
    for index in range(chunks_served):
        reply, _ = edge.response(_chunk_request(index))
        assert reply is not None
    return edge


def test_v3_poll_ok_mid_transfer_is_answered_not_final(tmp_path):
    # Half-sent uplink (chunk 0 of 3 served) and an OK lands — that is a connection
    # poll (the Hub re-polling, e.g. after a reboot), NOT the final-OK. It must get
    # its keepalive answer, and the partial file must not be marked sent (Edge.serve
    # would retire it and the upload would silently vanish).
    edge = _served_edge(tmp_path, chunks_served=1)

    reply, _ = edge.response(_request(Packet_v3.set_ok))

    assert reply is not None and reply.get_command() == Packet_v3.OK
    assert not edge.file.sent


def test_v3_final_ok_after_tail_chunk_finalizes_silently(tmp_path):
    # All 3 chunks served, then OK: that is the initiator's fire-and-forget final-OK.
    # It ends the transfer and nobody listens for a reply, so none is sent.
    edge = _served_edge(tmp_path, chunks_served=3)

    reply, _ = edge.response(_request(Packet_v3.set_ok))

    assert reply is None
    assert edge.file.sent


def test_v2_poll_ok_is_always_answered(tmp_path):
    # Legacy shape, unchanged: a v2 requester listens for the OK answer to its poll
    # (ask_ok times out otherwise). v2 also finalizes on it — that is v2's own
    # documented behavior and the shims must not change it.
    edge = _make_v2_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(500)),
                              chunk_size=edge.get_chunk_size()))

    chunk_req = Packet(mesh_mode=False, short_mac=True)
    chunk_req.set_source(HUB_MAC)
    chunk_req.set_destination(EDGE_MAC)
    chunk_req.ask_data(0)
    reply, _ = edge.response(chunk_req)
    assert reply is not None

    ok_req = Packet(mesh_mode=False, short_mac=True)
    ok_req.set_source(HUB_MAC)
    ok_req.set_destination(EDGE_MAC)
    ok_req.set_ok()
    reply, _ = edge.response(ok_req)

    assert reply is not None and reply.get_command() == Packet.OK
    assert edge.file.sent

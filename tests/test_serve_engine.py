"""Serve-engine behavior at the responder's public seam: `response` (one reply per
request) and `send_file` (the whole-file serve loop). These pin the regressions the
role-reversal review confirmed in the shared engine — behaviors both an Edge serving
its uplink and a Hub serving a delegated downlink rely on.
"""
import json
import threading
import time

import pytest

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


class _Stepping_loopback(Loopback_connector):
    """A loopback whose every receive window consumes a fixed slice of (fake) time and
    hands back a junk frame, so a serve loop spins deterministically without real blocking."""

    def __init__(self, mac, clock, step_ms=300):
        super().__init__(mac)
        self._clock = clock
        self._step_ms = step_ms

    def recv(self, focus_time=12):
        self._clock["now"] += self._step_ms   # a receive window took step_ms of wall time
        return b"\xff"                          # junk -> unparseable -> respond returns None


def test_send_file_timeout_is_measured_in_seconds(tmp_path, monkeypatch):
    # send_file(timeout=) must be SECONDS, like serve()/listen_to_endpoint() — not the v2-era
    # milliseconds. A 1-second timeout has to let ~1s of receive windows pass before giving up;
    # under the old ms reading it would bail after the very first window (~300 ms).
    clock = {"now": 100000}
    monkeypatch.setattr("AlLoRa.Nodes.Node.time", lambda: clock["now"])

    config_path = str(tmp_path / "edge.json")
    _write_config(config_path)
    conn = _Stepping_loopback(EDGE_MAC, clock, step_ms=300)
    edge = Edge(conn, config_file=config_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(500)),
                              chunk_size=edge.get_chunk_size()))

    start = clock["now"]
    edge.send_file(timeout=1)
    elapsed_ms = clock["now"] - start

    assert 1000 < elapsed_ms < 2000, (
        "timeout=1 elapsed {} ms; expected ~1 s of windows (seconds), not a "
        "sub-second bail (milliseconds)".format(elapsed_ms))


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


# --- the connection wait is v2 only, and says so at the call ------------------

def test_establish_connection_refuses_on_a_v3_node(tmp_path):
    # It negotiates over v2 RF-change fields that the v3 frame does not carry. Left
    # unguarded it ran until the first packet arrived and then raised AttributeError
    # from inside the receive loop, which reads as a library bug rather than a wrong
    # call. A v3 node must be told at the call, before the radio starts.
    edge = _make_edge(tmp_path)
    edge.connector.inbox.put(b"\xff")    # a packet would only make it fail later

    with pytest.raises(NotImplementedError) as excinfo:
        edge.establish_connection(try_for=1)

    assert "send_file" in str(excinfo.value), "the refusal must name the v3 way to wait"


def test_establish_connection_still_runs_on_a_v2_node(tmp_path):
    # The eight Edge examples that call it all default to version 2, so the guard must
    # be scoped to v3 and leave the legacy path exactly as it was.
    legacy = _make_v2_edge(tmp_path)
    legacy.connector.inbox.put(b"\xff")

    assert legacy.establish_connection(try_for=1) is False


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

    # The connection wait is v2 only, so it has to be probed on a v2 node.
    legacy = _make_v2_edge(tmp_path)
    capsys.readouterr()
    legacy.connector.inbox.put(b"\xff")    # instant round, nothing parseable
    legacy.establish_connection(try_for=1)

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


# --- RF_CONFIG trial: the serve-side commit criterion is a full-payload exchange ----
#
# A verified RF_CONFIG switch arms a trial: the node runs on the new config and must
# decide whether it STICKS (commit: persist the new config as last-known-good) or is
# unreachable (restore: fall back to last-known-good). v2 committed on the FIRST served
# frame (a metadata/OK poll), a false-positive: a short control frame closing does not
# prove a full max-payload chunk (long time-on-air) will land. The commit criterion is a
# COMPLETED full-payload exchange — the Hub asking for the *next* chunk (the prior full
# chunk demodulated) or the fire-and-forget final-OK (the whole file landed).


class _Trial_spy_edge(Edge):
    """An Edge that records the trial's terminal transitions — commit (backup_config,
    the new config persisted as last-known-good) and restore (revert to last-known-good)
    — so a test can assert which one a request stream drove, without reaching inside.
    `restored_at` is the monotonic stamp of the restore, for the tests that care when it
    landed and not only that it did."""

    def __init__(self, *args, **kwargs):
        self.committed = 0
        self.restored = 0
        self.restored_at = None
        super().__init__(*args, **kwargs)

    def backup_config(self):
        self.committed += 1
        super().backup_config()

    def restore_rf_config(self):
        self.restored += 1
        self.restored_at = time.monotonic()
        super().restore_rf_config()


def _make_spy_edge(tmp_path):
    config_path = str(tmp_path / "edge_trial.json")
    _write_config(config_path)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return _Trial_spy_edge(conn, config_file=config_path)


def _armed_edge_with_file(tmp_path, chunks_bytes=500):
    # An Edge that has just applied a verified RF_CONFIG (trial armed) and is serving a
    # multi-chunk uplink on the new config.
    edge = _make_spy_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(chunks_bytes)),
                              chunk_size=edge.get_chunk_size()))
    edge.change_rf_config({"sf": 9})     # arms the trial (connector now on sf9)
    edge.committed = 0                   # ignore any bookkeeping the arm itself did
    assert edge.sf_trial, "change_rf_config must arm the trial"
    return edge


def test_trial_does_not_commit_on_the_first_served_frame(tmp_path):
    # The metadata poll and the first chunk request are short exchanges: neither proves a
    # full max-payload chunk will land, so serving them must leave the trial armed.
    edge = _armed_edge_with_file(tmp_path)

    edge.response(_request(Packet_v3.ask_metadata))
    edge.response(_chunk_request(0))

    assert edge.committed == 0, "a short first exchange must not commit the trial"
    assert edge.sf_trial, "the trial must stay armed until a full exchange completes"


def test_trial_commits_when_the_hub_asks_for_the_next_chunk(tmp_path):
    # Serving chunk 0, then the Hub asking for chunk 1: the request for a later chunk
    # proves the prior full-payload chunk was demodulated on the new config. Commit —
    # the new config becomes last-known-good (persisted), the trial ends.
    edge = _armed_edge_with_file(tmp_path)

    edge.response(_chunk_request(0))
    assert edge.committed == 0, "one chunk is not yet proof — the Hub has not advanced"

    edge.response(_chunk_request(1))

    assert edge.committed == 1, "advancing to the next chunk must commit the trial once"
    assert not edge.sf_trial, "a committed trial is over"
    assert edge.restored == 0, "commit must not also restore"


def test_trial_commit_is_idempotent_across_further_chunks(tmp_path):
    # Once committed, later chunk requests must not re-commit (no repeated config persists).
    edge = _armed_edge_with_file(tmp_path)

    edge.response(_chunk_request(0))
    edge.response(_chunk_request(1))
    edge.response(_chunk_request(2))

    assert edge.committed == 1, "the trial commits exactly once, not on every later chunk"


def test_trial_does_not_commit_on_a_re_requested_chunk(tmp_path):
    # A stalled transfer (the Hub re-asking the SAME chunk, its last one never landing)
    # is not progress: the trial must stay armed, later to be resolved by the window.
    edge = _armed_edge_with_file(tmp_path)

    edge.response(_chunk_request(0))
    edge.response(_chunk_request(0))
    edge.response(_chunk_request(0))

    assert edge.committed == 0, "re-requesting the same chunk is a stall, not a commit"
    assert edge.sf_trial, "a stalled trial stays armed"


def _armed_edge_single_chunk(tmp_path):
    # A one-chunk uplink (smaller than chunk_size): no "next chunk" is ever asked, so the
    # only full-exchange proof is the final-OK after the tail.
    edge = _make_spy_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="tiny.bin", content=bytearray(bytes(100)),
                              chunk_size=edge.get_chunk_size()))
    edge.change_rf_config({"sf": 9})
    edge.committed = 0
    assert edge.get_chunk_size() > 100 and edge.file.get_length() == 1
    return edge


def test_trial_commits_on_the_final_ok_after_the_tail_chunk(tmp_path):
    # A single-chunk file: serve the (tail) chunk 0, then the fire-and-forget final-OK.
    # The whole file landed on the new config — commit.
    edge = _armed_edge_single_chunk(tmp_path)

    edge.response(_chunk_request(0))
    assert edge.committed == 0, "serving the only chunk is not yet proof it was demodulated"

    reply, _ = edge.response(_request(Packet_v3.set_ok))

    assert reply is None, "the final-OK after the tail is fire-and-forget (no reply)"
    assert edge.committed == 1, "the final-OK proves the file landed — commit"
    assert not edge.sf_trial


def test_trial_holds_on_a_mid_transfer_ok_poll(tmp_path):
    # A multi-chunk file, one chunk served, then an OK that is a connection poll (not the
    # final-OK — the tail was never served). That proves nothing about a full chunk landing:
    # the trial must stay armed (hold pending).
    edge = _armed_edge_with_file(tmp_path)

    edge.response(_chunk_request(0))
    reply, _ = edge.response(_request(Packet_v3.set_ok))

    assert reply is not None and reply.get_command() == Packet_v3.OK, "a poll gets its keepalive"
    assert edge.committed == 0, "a mid-transfer OK poll is not a full-exchange commit"
    assert edge.sf_trial, "the trial holds pending until a real full exchange resolves it"


# --- RF_CONFIG trial: the serve loop self-restores on a silent (unreachable) window -----

def test_serve_restores_last_known_good_after_a_silent_trial_window(tmp_path):
    # The deployed Edge home loop (serve) had NO trial→restore — only the legacy send_file
    # loop did (the shared node base). A verified RF_CONFIG that makes the Edge unreachable (nothing is
    # heard on the new config) must still self-heal: after the trial window of silence, the
    # serve loop falls back to the last-known-good config. The `trial` seconds ride the
    # (signed) payload; change_rf_config reads them.
    edge = _make_spy_edge(tmp_path)
    edge.change_rf_config({"sf": 9, "trial": 1})     # new config sf9, window 1s
    assert edge.connector.get_rf_config()[1] == 9 and edge.sf_trial

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 4},
                              name="edge-serve", daemon=True)
    server.start()
    server.join(timeout=8)
    assert not server.is_alive(), "serve did not return"

    assert edge.restored == 1, "a silent trial window must restore the last-known-good config"
    assert edge.connector.get_rf_config()[1] == 7, "the connector reverted to last-known-good sf7"
    assert not edge.sf_trial, "a restored trial is over"


# --- RF_CONFIG trial: the window has a ceiling no message can push out -----------------
#
# Every short control frame heard on the new config holds the trial, pushing the restore
# deadline out, and nothing capped that. A peer whose downlink can ask but whose uplink
# keeps losing the reply re-asks METADATA forever, and each ask extends the window: the node
# then neither commits (no chunk ever moves) nor restores (it is never silent), and sits on
# an unproven config for as long as the asking goes on. No attacker is needed for this, only
# an asymmetric link. A second deadline, armed with the first and moved by nothing, bounds
# the whole trial at three windows.


def _held_trial_edge(tmp_path, window_s=1):
    edge = _make_spy_edge(tmp_path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(500)),
                              chunk_size=edge.get_chunk_size()))
    edge.change_rf_config({"sf": 9, "trial": window_s})
    edge.committed = 0
    assert edge.sf_trial and edge.connector.get_rf_config()[1] == 9
    return edge


def _metadata_poller(edge, stop, period=0.3):
    # The peer re-asks METADATA well inside the window, so the window alone never elapses.
    # Nothing drains the edge's outbox, which is the lost uplink that keeps it asking.
    wire = edge.connector.codec.frame(_request(Packet_v3.ask_metadata))

    def keep_asking():
        while not stop.is_set():
            edge.connector.inbox.put(wire)
            time.sleep(period)

    return threading.Thread(target=keep_asking, name="metadata-poller", daemon=True)


def test_a_trial_held_by_repeated_polls_restores_at_the_ceiling(tmp_path):
    # The peer polls for the whole run and never asks for a chunk, so nothing here can ever
    # commit and every poll holds. The restore can only come from the ceiling.
    edge = _held_trial_edge(tmp_path, window_s=1)     # window 1 s, so the ceiling is 3 s
    stop = threading.Event()
    poller = _metadata_poller(edge, stop)

    started = time.monotonic()
    poller.start()
    server = threading.Thread(target=edge.serve, kwargs={"timeout": 6},
                              name="edge-serve", daemon=True)
    server.start()
    server.join(timeout=20)
    stop.set()

    assert not server.is_alive(), "serve did not return"
    assert edge.committed == 0, "no chunk ever moved, so nothing could have committed"
    assert edge.restored == 1, (
        "a trial held by a poll that never stops must still end: the config was never proven")
    assert edge.connector.get_rf_config()[1] == 7, "the connector reverted to last-known-good sf7"
    assert not edge.sf_trial, "a restored trial is over"
    assert 2.0 < (edge.restored_at - started) < 5.5, (
        "the restore landed {:.1f} s in; a 1 s window puts the ceiling at 3 s".format(
            edge.restored_at - started))


def test_a_poll_still_holds_the_trial_below_the_ceiling(tmp_path):
    # The other side of the same bound: the ceiling must not swallow the hold it caps. A
    # reachable but idle peer still gets its window extended, so the trial outlives the plain
    # window and only ends at the ceiling above it.
    edge = _held_trial_edge(tmp_path, window_s=1)
    stop = threading.Event()
    poller = _metadata_poller(edge, stop)

    poller.start()
    # Past the 1 s window (an unheld trial would already have restored) and clear of the 3 s
    # ceiling, so the assertion below is about the hold and nothing else.
    server = threading.Thread(target=edge.serve, kwargs={"timeout": 1.5},
                              name="edge-serve", daemon=True)
    server.start()
    server.join(timeout=20)
    stop.set()

    assert not server.is_alive(), "serve did not return"
    assert edge.restored == 0, (
        "the polls were heard on the new config, so the trial must outlive its plain window")
    assert edge.sf_trial, "a held trial is still armed below the ceiling"
    assert edge.connector.get_rf_config()[1] == 9, "the node is still on the trial config"

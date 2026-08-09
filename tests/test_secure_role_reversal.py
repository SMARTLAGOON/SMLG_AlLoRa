"""Acceptance - v3 role reversal under the `secure` posture.

The open-mode acceptance suite (test_role_reversal.py) proves the delegation state machine:
the Hub grants, the Edge pulls, every failure converges home. This suite proves the same
machine still holds when every frame is AEAD-sealed, and pins the two things that only
exist once crypto is on:

  * A forged GRANT cannot start a pull. In open mode anyone who knows the sid can make an
    Edge burn a downlink window driving at a Hub that is not serving; the tag closes that.
  * A rebooted Edge loses its RAM-only session, so reconverging is not just "resume the
    poll" as in open mode. The Edge cannot even report the problem, because to it a frame
    sealed under the old key is unparseable and unparseable is indistinguishable from
    silence, so the Hub has to notice by itself and re-handshake before anything can move.
    Which it cannot do from silence alone, since an Edge with nothing to send is silent on
    purpose: see the liveness cases at the end of this file.

Only cases whose path actually runs through the Codec or a Session are mirrored here. The
open suite's backoff, pacing, swap-id seeding, buffer-hygiene and ctor cases exercise no key
material, so duplicating them under secure would cost suite time on every run and imply a
coverage the duplicate does not add.

Reversal itself needs no crypto work, and that is by construction rather than by luck: a
Session seals with `send_nonce_prefix` and opens with `recv_nonce_prefix`, both keyed to
"me to peer", never to "source to collector". Moving the drive role is invisible to the
nonce, counter and replay layers, so the frames a delegated pull exchanges are ordinary
session frames in the ordinary direction.
"""
import json
import os
import queue
import threading
import time

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.DataSinks.DataSink import DataSink
from AlLoRa.File import AlLoRa_File
from AlLoRa.Security.handshake import initiator_hello, responder_accept, initiator_complete
from AlLoRa.Security.ec_p256 import generate_private_key

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
SESSION_ID = 42        # the Edge's sid: the session's address in both directions
HUB_OWN_SID = 7        # deliberately different, so sid-mirroring bugs surface

GRANT_KIND = 0x4
METADATA_KIND = 0x3
CHUNK_KIND = 0x2
OK_KIND = 0x1


class Filtered_loopback(Loopback_connector):
    """A loopback whose transmit drops frames of a chosen v3 kind. The kind sits in the low
    nibble of the byte at offset 1 in *both* postures (open `[sid][VT][FL][integ3]` and
    secure `[sid][VT][FL][counter2]`), because the secure header stays cleartext and is
    bound as the AEAD's AAD rather than encrypted. So the same targeted-loss filter the open
    suite uses works here untouched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.drop_rules = {}     # kind_code -> frames still to drop (-1 = drop forever)

    def drop_kind(self, kind_code, count=1):
        self.drop_rules[kind_code] = count

    def transmit(self, wire):
        kind = (wire[1] & 0x0F) if len(wire) >= 2 else None
        left = self.drop_rules.get(kind, 0)
        if left:
            if left > 0:
                self.drop_rules[kind] = left - 1
            self.dropped += 1
            return True          # "sent" fine; the channel ate it
        return super().transmit(wire)


def _make_filtered_pair():
    a_to_b, b_to_a = queue.Queue(), queue.Queue()
    return (Filtered_loopback(EDGE_MAC, inbox=b_to_a, outbox=a_to_b),
            Filtered_loopback(HUB_MAC, inbox=a_to_b, outbox=b_to_a))


class Capture_sink(DataSink):
    """A DataSink that keeps every delivered file in memory (name, bytes, reception)."""

    def __init__(self):
        self.received = []

    def consume(self, file, reception=None):
        self.received.append((file.get_name(), bytes(file.get_content()), reception))
        file.discard()


def _write_config(path, result_path, session_id):
    config = {
        "name": "rr-secure",
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "secure",
        "session_id": session_id,
        "debug": False,
        "result_path": result_path,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _pre_shared_sessions(sid):
    """The matching per-direction sessions the two ends derive on first contact, produced by
    running the real ECDH handshake up front. The Edge is the handshake initiator; the Hub is
    the responder (it holds the static key and assigns the sid)."""
    static_priv = generate_private_key(os.urandom)
    state, hello = initiator_hello(os.urandom)                        # Edge -> Hub
    session_r, welcome = responder_accept(static_priv, hello, sid)    # Hub (responder)
    session_i = initiator_complete(state, welcome)                    # Edge (initiator)
    return session_i, session_r


def _grant_packet(swap_id):
    from AlLoRa.Packet_v3 import Packet_v3
    grant = Packet_v3(addressing="sid")
    grant.set_session(SESSION_ID)
    grant.set_grant(swap_id)
    return grant


def _sealed_grant(hub, swap_id):
    """The wire bytes of a GRANT sealed under the Hub's live session, i.e. exactly what the
    Hub itself would put on the air. Sealing consumes one of the Hub's send counters, which
    is the point: each injected GRANT is a fresh frame, not a replay."""
    session = hub.session_store.get(SESSION_ID)
    return _grant_packet(swap_id).get_secure_content(session, hub.aead)


def _make_pair(tmp_path, edge_conn, hub_conn, edge_sink=None, pre_share=True, **hub_kwargs):
    edge_config = str(tmp_path / "edge.json")
    hub_config = str(tmp_path / "hub.json")
    _write_config(edge_config, str(tmp_path / "edge_results"), SESSION_ID)
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)

    sink = edge_sink if edge_sink is not None else Capture_sink()
    edge = Edge(edge_conn, config_file=edge_config, data_sink=sink)
    hub = Hub(hub_conn, config_file=hub_config, **hub_kwargs)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC,
                                active=True, session_id=SESSION_ID)
    if pre_share:
        session_i, session_r = _pre_shared_sessions(SESSION_ID)
        edge.session_store.put(session_i)
        hub.session_store.put(session_r)
    return edge, hub, endpoint, sink


# --- only the authority drives the handshake ---------------------------------

def test_edge_with_no_session_abandons_the_pull_instead_of_handshaking(tmp_path):
    # The handshake is the Hub's to drive: it holds the static key and assigns the sid. An
    # Edge that finds no session for its captured Hub endpoint must abandon the pull and come
    # home, never run the Hub's side of the exchange. Running it would put both ends on the
    # initiator side, and it would address a peer the Edge only ever knows through the live
    # session, so there is no real address to send to.
    #
    # The wire cannot reach this state today: a GRANT is itself a sealed frame, so an Edge
    # with no session can never decrypt one. The guard is for the paths that can appear later
    # (a session dropped on counter exhaustion, an explicit re-key), where the cost of no
    # guard is the whole serve loop dying on an unhandled error.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, _, _, _ = _make_pair(tmp_path, edge_conn, hub_conn)

    edge.session_store.drop(SESSION_ID)
    assert edge.session_store.get(SESSION_ID) is None

    edge._grant_pending = 5      # as _on_grant would have left it
    edge.serve(timeout=2)        # must survive the honoured-but-impossible delegation

    assert edge.current_role == "source", "the Edge must come home after abandoning the pull"


# --- happy path: sealed downlink byte-exact, then control returns to the Hub -

def test_secure_downlink_delivers_byte_exact_and_control_returns(tmp_path):
    payload = bytes((i * 7) % 256 for i in range(1000))   # 5 chunks at 243
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)

    hub.queue_downlink(endpoint, AlLoRa_File(name="model.bin",
                                             content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    assert hub.downlink_pending(endpoint)

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 16},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=12, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive(), "edge serve loop did not come home"

    assert [(n, c) for n, c, _ in sink.received] == [("model.bin", payload)]
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


def test_secure_first_contact_then_delegation(tmp_path):
    # No pre-shared session: the Hub runs the live ECDH handshake on its first drive, and the
    # delegation rides the session that handshake just established. Downlink-on-first-contact
    # is the realistic bring-up order for a node whose first job is to be configured.
    payload = bytes((i * 11) % 256 for i in range(700))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           pre_share=False, reclaim_timeout=3)
    assert hub.session_store.get(SESSION_ID) is None

    hub.queue_downlink(endpoint, AlLoRa_File(name="fc.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=12)
    assert not server.is_alive()

    assert hub.session_store.get(SESSION_ID) is not None, "the handshake never established"
    assert [(n, c) for n, c, _ in sink.received] == [("fc.bin", payload)]
    assert not hub.downlink_pending(endpoint)


# --- reclaim on lost GRANT ---------------------------------------------------

def test_secure_reclaim_on_lost_grant_then_redelivery(tmp_path):
    payload = bytes(i % 256 for i in range(500))   # 3 chunks at 243
    edge_conn, hub_conn = _make_filtered_pair()
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=2)

    hub.queue_downlink(endpoint, AlLoRa_File(name="cfg.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    hub_conn.drop_kind(GRANT_KIND, count=1)   # the first GRANT is lost in the channel

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=12)
    assert not server.is_alive()

    assert hub_conn.dropped == 1, "the GRANT drop never happened - filter inert"
    # The lost GRANT burned a send counter that the Edge never saw. The re-GRANT therefore
    # arrives with a gap in the counter sequence, which the replay window must accept (it
    # rejects repeats and stale values, not gaps) or a single lost frame would wedge the link.
    assert [(n, c) for n, c, _ in sink.received] == [("cfg.bin", payload)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# --- reclaim on lost first pull ----------------------------------------------

def test_secure_reclaim_on_lost_first_pull_then_recovery(tmp_path):
    payload = bytes((i * 5) % 256 for i in range(500))
    edge_conn, hub_conn = _make_filtered_pair()
    # The GRANT lands, but the Edge's first pulls die in the channel: the Hub serves silence,
    # so only its reclaim timer can end the delegation.
    edge_conn.drop_kind(METADATA_KIND, count=4)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=1.0)

    hub.queue_downlink(endpoint, AlLoRa_File(name="relay.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    first_swap_id = hub._swap_id

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 24},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=20, save_file=True)
    server.join(timeout=14)
    assert not server.is_alive()

    assert edge_conn.dropped >= 4, "the pull drops never happened - filter inert"
    assert (hub._swap_id - first_swap_id) & 0xFF >= 2, "the reclaim timer never fired"
    assert [(n, c) for n, c, _ in sink.received] == [("relay.bin", payload)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# --- fielded firmware: the legacy set_file + send_file loop ------------------

def test_secure_grant_honored_from_legacy_send_file_loop(tmp_path):
    # Deployed Edges run `while True: set_file(); send_file()` and never call Edge.serve.
    # Under secure this loop also has a live CTRL branch that open mode never exercises: a
    # handshake frame can legitimately land in the middle of serving, so the responder handler
    # must keep routing CTRL to the handshake and data to the serve path while a delegation
    # is in flight.
    down = bytes((i * 19) % 256 for i in range(500))
    up = bytes((i * 23) % 256 for i in range(500))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub_sink = Capture_sink()
    edge, hub, endpoint, edge_sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                                reclaim_timeout=2, data_sink=hub_sink)

    hub.queue_downlink(endpoint, AlLoRa_File(name="down.bin", content=bytearray(down),
                                             chunk_size=hub.get_chunk_size()))

    def fielded_main():
        deadline = time.time() + 25
        while not edge_sink.received and time.time() < deadline:
            if not edge.got_file():
                edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(up),
                                          chunk_size=edge.get_chunk_size()))
            edge.send_file(timeout=5)       # seconds
    server = threading.Thread(target=fielded_main, name="edge-legacy", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=30)
    assert not server.is_alive()

    assert [(n, c) for n, c, _ in edge_sink.received] == [("down.bin", down)]
    assert ("up.bin", up) in [(n, c) for n, c, _ in hub_sink.received]
    assert not hub.downlink_pending(endpoint)


# --- secure-only: an unsealed GRANT cannot start a pull ----------------------

def test_forged_unsealed_grant_never_starts_a_pull(tmp_path):
    # The reason the catastrophic direction wants the secure posture. A GRANT is
    # fire-and-forget and carries no proof of its own in open mode, so anyone who learns the
    # sid can spend an Edge's downlink window (and its battery) driving at a Hub that is not
    # serving. Sealed, the same frame fails the tag and is never parsed, so the Edge stays
    # home and keeps answering the real Hub.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn)

    # Framed open, as an attacker without the session key can only do.
    hub_conn.transmit(_grant_packet(9).get_content())
    edge.serve(timeout=3)

    assert edge.current_role == "source", "a forged GRANT moved the Edge off its home role"
    assert edge_conn.outbox.empty(), "the Edge pulled on an unauthenticated GRANT"
    assert sink.received == []


# --- Hub-authority tie-break -------------------------------------------------

def test_secure_hub_authority_tie_break_edge_yields_to_poll(tmp_path):
    # A dual-initiator overlap, forced the realistic way: the Hub "rebooted" right after a
    # GRANT went out (it lost the delegation and just polls) while the Edge honors that GRANT
    # and starts driving. Hub always wins. Under secure the yield signal has to survive the
    # crypto: the Edge only recognises the contending poll as an OK once the frame has been
    # opened under its session, so the tie-break rides on a decrypt, not on a cleartext kind.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn)

    hub_conn.transmit(_sealed_grant(hub, 9))     # the stale GRANT, as the pre-reboot Hub sent it

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 12},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=8, save_file=True)
    server.join(timeout=8)
    assert not server.is_alive(), "edge never yielded - it sat out the whole drive window"

    assert edge.yield_count >= 1
    assert sink.received == []               # nothing was ever there to pull
    assert edge.current_role == "source"
    assert hub.current_role == "collector"
    assert endpoint.state == Digital_Endpoint.REQUEST_DATA_STATE


# --- GRANT dedupe under secure: two distinct mechanisms ----------------------

def test_secure_duplicate_grant_after_completed_pull_is_ignored(tmp_path):
    # After a COMPLETED pull, a stray repeat of the same GRANT must not buy a second pull.
    # Under secure that splits into two independent defences, and both are worth pinning
    # because each covers what the other cannot:
    #   * a byte-identical repeat is a replay, killed by the session's replay window before
    #     the delegation logic ever sees it;
    #   * a freshly sealed frame carrying the already-completed swap id passes the replay
    #     window legitimately (its counter is new), so only the Edge's own id dedupe stops it.
    payload = bytes((i * 29) % 256 for i in range(300))
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                           reclaim_timeout=3)
    hub.queue_downlink(endpoint, AlLoRa_File(name="one.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))
    server = threading.Thread(target=edge.serve, kwargs={"timeout": 14},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=10, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()
    assert [(n, c) for n, c, _ in sink.received] == [("one.bin", payload)]

    while not edge_conn.outbox.empty():
        edge_conn.outbox.get_nowait()

    # A fresh frame, stale id: the replay window lets it through, the id dedupe must not.
    hub_conn.transmit(_sealed_grant(hub, hub._swap_id))
    edge.serve(timeout=3)
    assert edge_conn.outbox.empty(), "the Edge pulled again on a re-sealed duplicate GRANT"

    # A byte-identical repeat: rejected one layer earlier, by the replay window.
    replayed = _sealed_grant(hub, hub._swap_id)
    hub_conn.transmit(replayed)
    edge.serve(timeout=2)
    hub_conn.transmit(replayed)
    edge.serve(timeout=2)
    assert edge_conn.outbox.empty(), "the Edge pulled on a replayed GRANT frame"


# --- reboot to home role, and back into a session ----------------------------

def _visit_loop(hub, endpoint, visits, listening_time, save_file=True):
    """Drive a run of bounded visits, the way every real caller does: the Gateway's scheduler
    revisits each endpoint for `ep.listening_time` at a time, and the 1:1 examples loop
    `listen_to_endpoint(endpoint, 60)`. Nothing calls it once and forever, which is why a
    per-visit budget is the granularity the Hub can actually count in."""
    def run():
        for _ in range(visits):
            hub.listen_to_endpoint(endpoint, listening_time=listening_time,
                                   save_file=save_file)
    thread = threading.Thread(target=run, name="hub-drive", daemon=True)
    thread.start()
    return thread


def _reboot_scenario(tmp_path, visits=8, visit_time=5, **hub_kwargs):
    """Drive an Edge to death mid-pull, then bring a fresh one up on the same radio and let
    the Hub keep revisiting. Returns (hub, endpoint, edge2, sink2, payload)."""
    payload = bytes((i * 11) % 256 for i in range(500))
    edge_conn, hub_conn = _make_filtered_pair()
    edge1, hub, endpoint, sink1 = _make_pair(tmp_path, edge_conn, hub_conn,
                                             reclaim_timeout=1.2, **hub_kwargs)
    edge1.downlink_window = 1.5   # give up a dead pull quickly (test pacing)

    hub.queue_downlink(endpoint, AlLoRa_File(name="fw.bin", content=bytearray(payload),
                                             chunk_size=hub.get_chunk_size()))

    # Edge1 takes the GRANT and pulls METADATA, then its drive dies (every CHUNK request is
    # lost) until "power-off": serve() returns and the instance is abandoned.
    edge_conn.drop_kind(CHUNK_KIND, count=-1)
    first = _visit_loop(hub, endpoint, visits=1, listening_time=6)
    edge1.serve(timeout=3)
    assert edge1.current_role == "source"    # it came home before "dying"
    assert sink1.received == []
    first.join(timeout=20)

    # Reboot: the channel heals and a fresh Edge starts on the same radio. Home role, no
    # memory of the granted pull, and no session.
    edge_conn.drop_rules.clear()
    sink2 = Capture_sink()
    edge2 = Edge(edge_conn, config_file=str(tmp_path / "edge.json"), data_sink=sink2)
    assert edge2.current_role == "source" == edge2.home_role
    assert edge2.session_store.get(SESSION_ID) is None, "a reboot must lose the RAM session"

    rest = _visit_loop(hub, endpoint, visits=visits, listening_time=visit_time)
    edge2.serve(timeout=visits * visit_time + 12)
    rest.join(timeout=visits * visit_time + 20)
    assert not rest.is_alive()
    return hub, endpoint, edge2, sink2, payload


def test_secure_edge_reboot_mid_swap_lands_home_and_reconverges(tmp_path):
    # No swap state is persisted, so the rebooted Edge is back at its home serve role. Under
    # secure it also comes back with an empty session store, because sessions are RAM-only by
    # design, and that is the part the Hub has to notice: it still holds its own half of a
    # session the Edge cannot use, and it is the only side that can offer a new one.
    #
    # Without recovery the pair is stranded, and not for want of trying by either side: the Hub
    # seals polls under a key the Edge no longer holds, and to the Edge an unopenable frame is
    # indistinguishable from silence, so it never answers and never asks. The Hub's liveness
    # budget breaks that by tearing down the session it can no longer use, which puts the
    # endpoint back in the same state as one that has never been contacted.
    hub, endpoint, edge2, sink2, payload = _reboot_scenario(tmp_path)

    assert edge2.current_role == "source" == edge2.home_role
    assert hub.current_role == "collector"
    # A fresh session on both sides: the pair re-handshook rather than limping on.
    assert edge2.session_store.get(SESSION_ID) is not None
    assert hub.session_store.get(SESSION_ID) is not None
    # And the downlink that was pending across the whole outage still landed, exactly once.
    assert [(n, c) for n, c, _ in sink2.received] == [("fw.bin", payload)]
    assert not hub.downlink_pending(endpoint)


# --- return to normal: downlink, then a regular uplink in the same session ---

def test_secure_uplink_still_normal_after_downlink_reversal(tmp_path):
    # The role flip must leave the session exactly as it found it. Both ends keep counting on
    # the same send counters across the reversal (the counters belong to a direction, not to a
    # role), so if the flip disturbed the nonce or replay bookkeeping the following uplink is
    # where it would show up, as frames that no longer open.
    down = bytes(i % 256 for i in range(700))          # 3 chunks at 243
    up = bytes((255 - i) % 256 for i in range(600))    # 3 chunks at 243
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    hub_sink = Capture_sink()
    edge, hub, endpoint, edge_sink = _make_pair(tmp_path, edge_conn, hub_conn,
                                                reclaim_timeout=3, data_sink=hub_sink)

    hub.queue_downlink(endpoint, AlLoRa_File(name="down.bin", content=bytearray(down),
                                             chunk_size=hub.get_chunk_size()))
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(up),
                              chunk_size=edge.get_chunk_size()))

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 20},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=16, save_file=True)
    server.join(timeout=10)
    assert not server.is_alive()

    assert [(n, c) for n, c, _ in edge_sink.received] == [("down.bin", down)]
    assert [(n, c) for n, c, _ in hub_sink.received] == [("up.bin", up)]
    assert hub.current_role == "collector"
    assert edge.current_role == "source"
    assert not hub.downlink_pending(endpoint)


# =============================================================================
# The control channel end to end: a signed artifact over a sealed delegation.
#
# Until now the two halves were only ever tested apart. The verify gate was fed envelopes
# directly and the Edge's drain was tested against a stubbed pull, so no signed control
# artifact had ever actually crossed the wire. These close that: the artifact is queued as an
# ordinary downlink, delegated, pulled sealed, verified against the control root, and only
# then actuated. The ordering is the point. Actuating inside consume() would switch the radio
# or reboot before the transfer's final-OK reaches the air, and the Hub would re-grant the
# same command forever.
# =============================================================================

from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from test_control_root_sink import (ENV_RF_CONFIG_VALID, ENV_RESET_VALID,
                                    CONTROL_ROOT_HEX, TARGET_DEVICE_ID)


class _Probed_gate(Control_Root_DataSink):
    """The real verify gate, plus a snapshot taken the instant consume() returns. That is
    after the artifact has been verified and the actuator has queued its action, but before
    the drive loop puts the final-OK on the air, so it is exactly the window in which nothing
    is allowed to have happened yet."""

    def __init__(self, *args, **kwargs):
        self.probe = kwargs.pop("probe")
        super().__init__(*args, **kwargs)
        self.at_consume = []

    def consume(self, file, reception=None):
        try:
            return super().consume(file, reception)
        finally:
            self.at_consume.append(self.probe())


def _control_pair(tmp_path, envelope, reset_log=None, probe=None):
    """An Edge whose downlink sink is the real control-root gate wrapping the real actuator,
    with `envelope` queued on the Hub as a pending downlink."""
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge_config = str(tmp_path / "edge.json")
    hub_config = str(tmp_path / "hub.json")
    _write_config(edge_config, str(tmp_path / "edge_results"), SESSION_ID)
    _write_config(hub_config, str(tmp_path / "hub_results"), HUB_OWN_SID)

    edge = Edge(edge_conn, config_file=edge_config)
    hub = Hub(hub_conn, config_file=hub_config, reclaim_timeout=3)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC,
                                active=True, session_id=SESSION_ID)
    session_i, session_r = _pre_shared_sessions(SESSION_ID)
    edge.session_store.put(session_i)
    hub.session_store.put(session_r)

    actuator = Node_Control_Actuator(edge, reset_fn=(lambda: reset_log.append("reset"))
                                 if reset_log is not None else None)
    # device_id is what the node was provisioned with; here it is the frozen vectors' target.
    # The gate keeps its replay mark on the node's own filesystem, so give this node one: the
    # frozen envelopes all carry counter 1, and a mark shared between two of these pairs is two
    # different boards sharing one flash. The second would then correctly refuse a first command.
    gate = _Probed_gate(control_root=CONTROL_ROOT_HEX, device_id=TARGET_DEVICE_ID,
                        actuator=actuator, probe=probe or (lambda: None),
                        counter_file=str(tmp_path / "control.counter"))
    edge.data_sink = gate

    hub.queue_downlink(endpoint, AlLoRa_File(name="ctl.bin", content=bytearray(envelope),
                                             chunk_size=hub.get_chunk_size()))
    return edge, hub, endpoint, gate


def _run_delegation(edge, hub, endpoint, edge_timeout=16, hub_timeout=12):
    server = threading.Thread(target=edge.serve, kwargs={"timeout": edge_timeout},
                              name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=hub_timeout, save_file=True)
    server.join(timeout=12)
    assert not server.is_alive(), "edge serve loop did not come home"


def test_signed_rf_config_over_a_sealed_downlink_applies_after_the_final_ok(tmp_path):
    edge = None

    def probe():
        return (edge.connector.sf, edge._pending_control is not None)

    edge, hub, endpoint, gate = _control_pair(tmp_path, ENV_RF_CONFIG_VALID, probe=probe)
    assert edge.connector.sf == 7

    _run_delegation(edge, hub, endpoint)

    # Verified and deferred: at the end of consume the command was queued and the radio had
    # not moved, so the final-OK still went out on the config the Hub was listening on.
    assert gate.at_consume == [(7, True)], \
        "the radio moved (or nothing was queued) before the final-OK was sent"
    # Then drained: the signed envelope carries {"sf":9,"bw":125,"tx_power":14}.
    assert edge.connector.sf == 9, "the verified RF_CONFIG never reached the radio"
    assert edge.sf_trial, "applying a downlink RF_CONFIG must arm the self-restore trial"
    assert edge._pending_control is None, "the drained action must be cleared"
    # And the Hub heard completion, so it will not re-grant the same command.
    assert not hub.downlink_pending(endpoint)


def test_signed_reset_over_a_sealed_downlink_fires_after_the_final_ok(tmp_path):
    reset_log = []
    edge, hub, endpoint, gate = _control_pair(
        tmp_path, ENV_RESET_VALID, reset_log=reset_log,
        probe=lambda: list(reset_log))

    _run_delegation(edge, hub, endpoint)

    # A reboot inside consume() would cut the transfer before its final-OK, the Hub would
    # never hear completion, and it would re-grant the same RESET into a reboot loop.
    assert gate.at_consume == [[]], "the node reset before acknowledging the command"
    assert reset_log == ["reset"], "the verified RESET never actuated"
    assert not hub.downlink_pending(endpoint)


def test_forged_control_artifact_over_a_sealed_downlink_is_dropped_without_a_retry_loop(tmp_path):
    # A sealed link proves the artifact came from the Hub, not that the Hub was entitled to
    # command this node: the Hub relays what a backend minted and never signs anything itself.
    # So the control root still has to gate it. A bad signature must be dropped rather than
    # raised on, because raising withholds the final-OK and the Hub would re-serve the same
    # bad bytes forever.
    forged = bytearray(ENV_RF_CONFIG_VALID)
    forged[-1] ^= 0xFF                      # corrupt the signature's last byte
    edge = None

    def probe():
        return (edge.connector.sf, edge._pending_control is not None)

    edge, hub, endpoint, gate = _control_pair(tmp_path, bytes(forged), probe=probe)

    _run_delegation(edge, hub, endpoint)

    assert gate.at_consume == [(7, False)], "a forged artifact queued an action"
    assert edge.connector.sf == 7, "a forged RF_CONFIG reached the radio"
    assert not edge.sf_trial
    # Dropped, but still acknowledged: the delegation completed and nothing is re-queued.
    assert not hub.downlink_pending(endpoint)


# =============================================================================
# Session liveness: telling an idle Edge from one that can no longer hear us.
#
# The rule the reboot case above depends on, tested directly. It has to thread a needle:
# spend a session too eagerly and every idle sensor in a deployment pays an ECDH it did not
# need; spend it too reluctantly and a rebooted Edge stays stranded.
# =============================================================================

def test_secure_hub_keeps_the_session_of_an_idle_but_answering_edge(tmp_path):
    # The half of the liveness rule that costs real money if it is wrong. An Edge with nothing
    # to send answers nothing: it lets a metadata poll go by rather than spend airtime saying
    # "nothing yet", so an idle sensor looks exactly like a dead one here. Since re-keying is
    # an ECDH at both ends, reading that silence as a dead session would put every idle
    # endpoint in a deployment through a pointless re-handshake every few visits.
    #
    # So the run of silence only re-arms the connection poll, and answering it settles the
    # question. Same session object throughout is the strongest form of "never re-established".
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                        session_recovery_after=2)
    original = hub.session_store.get(SESSION_ID)
    assert original is not None

    server = threading.Thread(target=edge.serve, kwargs={"timeout": 30},
                              name="edge-serve", daemon=True)
    server.start()
    visits = _visit_loop(hub, endpoint, visits=5, listening_time=4)
    visits.join(timeout=45)
    server.join(timeout=40)

    assert hub.session_store.get(SESSION_ID) is original, \
        "an idle but reachable Edge had its session torn down and re-established"


def test_secure_hub_polls_before_spending_a_session_and_drops_when_unanswered(tmp_path):
    # And the other half: nothing is on the other end at all, so the re-armed poll goes
    # unanswered too. That is the signal worth acting on, and only then is the session spent.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    _, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                     session_recovery_after=2)
    assert hub.session_store.get(SESSION_ID) is not None

    hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)
    assert hub.session_store.get(SESSION_ID) is not None, \
        "one silent visit must not spend the session"

    hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)
    assert hub.session_store.get(SESSION_ID) is not None, \
        "reaching the budget must ask the question, not answer it"
    assert endpoint.state == Digital_Endpoint.OK, \
        "reaching the budget must re-arm the connection poll"

    hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)
    assert hub.session_store.get(SESSION_ID) is None, \
        "an unanswered connection poll must drop the session so the next visit re-handshakes"


def test_secure_session_teardown_clears_the_delegation_backoff(tmp_path):
    # Delegations that failed while the session was dead all failed for that one reason, so
    # the skip they earned is measuring a condition the teardown has just removed. If it
    # survives the repair it goes on deferring the pending downlink, which on this direction
    # can be a queued command, so the endpoint reconnects and still gets nothing.
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    _, hub, endpoint, _ = _make_pair(tmp_path, edge_conn, hub_conn,
                                     session_recovery_after=2)
    hub.queue_downlink(endpoint, AlLoRa_File(name="queued.bin", content=bytearray(bytes(60)),
                                             chunk_size=hub.get_chunk_size()))

    for _ in range(3):      # silent, silent (poll re-armed), silent (poll unanswered)
        hub.listen_to_endpoint(endpoint, listening_time=2, save_file=True)

    assert hub.session_store.get(SESSION_ID) is None, "the session should have been spent"
    assert SESSION_ID not in hub._delegation_backoff, \
        "the delegation skip outlived the session teardown that made it meaningless"
    assert hub.downlink_pending(endpoint), "the queued downlink must survive the teardown"

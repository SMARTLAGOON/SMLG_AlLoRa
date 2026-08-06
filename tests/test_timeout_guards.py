"""Unit — a request timeout on the drive side reads as "nothing came back", not as a crash.

`send_request` returns None for every round that produced no packet: a plain timeout, or a
frame the connector reported as an error. The pull verbs then dereferenced that None
(`response_packet.get_command()`), so each lost frame raised an AttributeError that the
drive loop swallowed and logged. The transfer still completed - the round retried on its
normal cadence - but it left a generic swallowed exception on the hottest path a lossy link
has, where a real fault prints the same line as an ordinary timeout and hides in the noise.

Guarding it puts a timeout back on the normal return path, so what the exception used to do
by accident has to be done on purpose. Two things rode on it:

  * the sleep controller. The except arm calls `Pacing.on_failure`; the normal arm ends in
    `Pacing.on_success`. A silent round has to keep counting as a failure, or the controller
    would hunt for a shorter inter-request gap on a link that is answering nothing.
  * the endpoint's escalate-to-mesh counter, which counts the failed rounds a timeout is the
    commonest case of. It never saw one, because the crash jumped over it.
"""
import json
import queue

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"
PEER_MAC = "a1a1a1a1"
SESSION_ID = 42


def _make_hub(tmp_path, mesh_mode=False, protocol_version=3):
    """A Hub with nobody on the other end: its inbox never fills, so every round times out."""
    config = {
        "name": "hub",
        "chunk_size": 243,
        "mesh_mode": mesh_mode,
        "short_mac": True,
        "protocol_version": protocol_version,
        "security_mode": "open",
        "session_id": 7,
        "debug": False,
        "result_path": str(tmp_path / "results"),
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    path = str(tmp_path / "hub.json")
    with open(path, "w") as f:
        json.dump(config, f)
    conn = Loopback_connector(HUB_MAC, inbox=queue.Queue(), outbox=queue.Queue())
    return Hub(conn, config_file=path)


def _endpoint():
    return Digital_Endpoint(name="peer", mac_address=PEER_MAC, active=True,
                            session_id=SESSION_ID)


# --- the pull verbs: a timeout is a return value, not an exception --------------------------

def test_ask_ok_reports_a_timeout_instead_of_raising(tmp_path):
    hub = _make_hub(tmp_path)
    hub.send_request = lambda packet: None          # the no-reply contract

    assert hub.ask_ok(hub.new_packet()) == (None, None)


def test_ask_metadata_reports_a_timeout_instead_of_raising(tmp_path):
    hub = _make_hub(tmp_path)
    hub.send_request = lambda packet: None

    assert hub.ask_metadata(hub.new_packet()) == (None, None)


def test_ask_data_reports_a_timeout_instead_of_raising(tmp_path):
    hub = _make_hub(tmp_path)
    hub.send_request = lambda packet: None

    assert hub.ask_data(hub.new_packet(), 0) == (None, None)


def test_ask_change_rf_gives_up_without_an_exception(tmp_path):
    # The in-band reconfiguration, which is what a v3 open Hub selects. A silent peer must
    # exhaust the attempts and report a refused change, not raise: the give-up value is the
    # only thing distinguishing "the Edge did not accept" from "the Edge accepted", and a
    # caller that got an exception instead would have no way to tell which.
    #
    # Retargeted deliberately when the v3 in-band transport landed. It used to reach the
    # legacy loop through the no-authority fallback, which on a v3 link put frames on the air
    # that no peer listens to; the v2 counterpart below now covers that loop where it is live.
    hub = _make_hub(tmp_path)
    hub.send_request = lambda packet: None

    assert hub.ask_change_rf(_endpoint(), {"sf": 9}) is False


def test_the_legacy_reconfig_loop_gives_up_without_an_exception_on_a_v2_hub(tmp_path):
    # The same guarantee for the v2 encoding, on the only kind of node that still speaks it.
    # The unguarded dereference lived here, inside a try/except that already spent a try on
    # it, so guarding it changed nothing except that a silent peer stopped printing a crash
    # for each of the twenty attempts.
    hub = _make_hub(tmp_path, protocol_version=2)
    hub.send_request = lambda packet: None

    assert hub.ask_change_rf(_endpoint(), {"sf": 9}) is False


# --- what the crash used to do, and now has to be done on purpose ---------------------------

def test_a_silent_visit_counts_failed_rounds_not_successful_ones(tmp_path):
    # The regression this guards: with the crash gone, a timed-out round falls through to the
    # success arm unless the loop asks whether anything actually replied.
    hub = _make_hub(tmp_path)
    calls = []
    hub.pacing.on_success = lambda: calls.append("success")
    hub.pacing.on_failure = lambda: calls.append("failure")

    hub.listen_to_endpoint(_endpoint(), listening_time=2)

    assert calls, "the visit ran no rounds at all"
    assert set(calls) == {"failure"}


def test_a_silent_round_counts_toward_the_endpoints_mesh_escalation(tmp_path):
    # A mesh-capable node escalates an endpoint to mesh after enough failed rounds. A timeout
    # is the commonest failed round there is, and it never reached the counter.
    hub = _make_hub(tmp_path, mesh_mode=True)
    endpoint = _endpoint()

    hub.listen_to_endpoint(endpoint, listening_time=2)

    assert endpoint.retransmission_counter > 0

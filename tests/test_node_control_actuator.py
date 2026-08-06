"""Unit — the Edge-side control actuator (the actuator behind the control-root gate).

The verifier (Control_Root_DataSink) authenticates a downlink control envelope and hands the
verified (control_type, payload) to an actuator. This is that actuator: it turns a verified
RF_CONFIG into a node RF-config change and a verified RESET into a hard reset.

The one behavior that is easy to get wrong and expensive on hardware: the actuator runs *inside*
consume(), which fires BEFORE the transfer's final-OK goes on the air (the node's drive loop).
Acting synchronously would switch the radio (or reboot) before the Hub is acknowledged -> a
missed final-OK, a stale config the Hub never hears, or a reset loop. So the actuator never
acts in apply(): it *queues* the action, and the Edge drains it after the pull completes, once
the final-OK is out. Mirrors v2's proven "reply on the old config, then switch."
"""

import json as _json

from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.Control.control_types import RF_CONFIG, RESET
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge

RF_PAYLOAD = b'{"sf":9,"bw":125,"tx_power":14}'
RF_PARSED = {"sf": 9, "bw": 125, "tx_power": 14}


class _FakeNode:
    """Records what the actuator asks of a node without touching a real radio: the queued
    deferred action (what the Edge would drain) and any change_rf_config that actually ran."""

    def __init__(self):
        self.rf_calls = []       # configs passed to change_rf_config (only on drain)
        self.queued = []         # deferred thunks handed to queue_control_action

    def change_rf_config(self, cfg):
        self.rf_calls.append(cfg)
        return True

    def queue_control_action(self, action):
        self.queued.append(action)


# --- Slice 1: a verified RF_CONFIG is queued, never applied synchronously in apply() --------

def test_rf_config_apply_queues_a_deferred_action_and_does_not_switch_now():
    node = _FakeNode()
    actuator = Node_Control_Actuator(node)

    actuator.apply(RF_CONFIG, RF_PAYLOAD)

    assert node.rf_calls == [], \
        "the actuator must NOT switch the radio inside apply() (that runs before the final-OK)"
    assert len(node.queued) == 1, "a verified RF_CONFIG must queue exactly one deferred action"


# --- Slice 2: draining the queued action applies the PARSED config -------------------------

def test_draining_the_queued_rf_config_action_applies_the_parsed_config():
    node = _FakeNode()
    actuator = Node_Control_Actuator(node)
    actuator.apply(RF_CONFIG, RF_PAYLOAD)

    node.queued[0]()   # the Edge drains it after the final-OK is on the air

    assert node.rf_calls == [RF_PARSED], \
        "draining must hand change_rf_config the parsed JSON config, not the raw bytes"


# --- Slice 3: RESET queues, and only resets on drain (never inside apply) -------------------

def test_reset_apply_queues_and_only_resets_on_drain():
    node = _FakeNode()
    reset_calls = []
    # Inject a fake reset so the suite never actually reboots; on-device this is machine.reset.
    actuator = Node_Control_Actuator(node, reset_fn=lambda: reset_calls.append(1))

    actuator.apply(RESET, b"")
    assert reset_calls == [], "RESET must not reboot inside apply() (that is before the final-OK)"
    assert len(node.queued) == 1, "a verified RESET must queue exactly one deferred action"

    node.queued[0]()   # the Edge drains it after the final-OK is on the air
    assert reset_calls == [1], "draining a queued RESET must invoke the reset function once"


# --- Slice 4: a malformed / non-dict RF_CONFIG payload is dropped, never queued or raised ---

def test_malformed_rf_config_payload_is_dropped_not_queued_and_does_not_raise():
    # A validly-signed but unparseable payload is a backend bug, not a transient failure. It must
    # NOT raise: raising re-pulls the identical bad bytes forever and withholds the final-OK. Drop
    # it (the anti-loop rule the verifier's reject already follows), queue nothing.
    node = _FakeNode()
    actuator = Node_Control_Actuator(node)

    actuator.apply(RF_CONFIG, b"not json at all {{{")   # must not raise
    assert node.queued == [], "unparseable JSON must not queue an action"

    actuator.apply(RF_CONFIG, b"[1, 2, 3]")             # valid JSON, but not an object -> not a config
    assert node.queued == [], "a non-dict payload must not queue an action"

    actuator.apply(RF_CONFIG, b"\xff\xfe\x00bad-bytes")  # not even decodable
    assert node.queued == [], "an undecodable payload must not queue an action"


# --- the Edge-level deferral seam ----------------------------------------------------------

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


def _make_edge(tmp_path):
    config = {
        "name": "edge", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 42, "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    path = str(tmp_path / "edge.json")
    with open(path, "w") as f:
        _json.dump(config, f)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=path)


# --- Slice 5: the Edge drains a queued action only after a completed delegated pull ---------

def test_service_grant_drains_the_queued_action_after_a_delivered_pull(tmp_path):
    edge = _make_edge(tmp_path)
    ran = []
    edge._grant_pending = 7

    def fake_pull():
        # A delivered pull: the verified artifact's consume() queued a control action, and by
        # the time the pull returns the transfer's final-OK is already on the air.
        edge.queue_control_action(lambda: ran.append("applied"))
        return True

    edge._pull_downlink = fake_pull
    edge._service_grant()

    assert ran == ["applied"], "a queued control action must be drained after a delivered pull"
    assert edge._pending_control is None, "the drained action must be cleared (never re-run)"


def test_service_grant_is_a_noop_when_nothing_is_pending(tmp_path):
    edge = _make_edge(tmp_path)
    edge._service_grant()   # no grant, no queued action: must not raise
    assert edge._pending_control is None


# --- Slice: the (signed) `trial` seconds ride the payload into the node's trial window -----

def test_draining_an_rf_config_with_trial_sets_the_node_trial_window(tmp_path):
    # The trial window is authoritative and carried in the signed RF_CONFIG payload as `trial`
    # (seconds). The actuator hands the whole parsed config to change_rf_config, which arms the
    # trial and reads the window from it — so a reconfig on a slow SF gets the backend-sized
    # window it needs, not a node-global guess.
    edge = _make_edge(tmp_path)
    actuator = Node_Control_Actuator(edge)

    actuator.apply(RF_CONFIG, b'{"sf":9,"bw":125,"tx_power":14,"trial":45}')
    edge._pending_control()      # drain, as the Edge does after the pull's final-OK

    assert edge.sf_trial, "draining a verified RF_CONFIG must arm the trial"
    assert edge._trial_window_s == 45, "the payload's `trial` seconds must become the window"


def test_rf_config_without_trial_falls_back_to_a_toa_scaled_default(tmp_path):
    # An omitted `trial` is legal: the node self-sizes a ToA-scaled default so a window-less
    # command neither commits on noise nor rolls back too eagerly.
    edge = _make_edge(tmp_path)
    actuator = Node_Control_Actuator(edge)

    actuator.apply(RF_CONFIG, b'{"sf":9}')
    edge._pending_control()

    assert edge.sf_trial
    assert edge._trial_window_s is None, "no explicit window was carried"
    assert edge._default_trial_window() >= 30.0, "the fallback window is a sane ToA-scaled floor"


# --- Slice 6: end-to-end gate -> actuator, on a genuinely signed envelope -------------------

def test_verified_envelope_through_the_gate_reaches_the_actuator_and_queues(tmp_path):
    # Compose the real verifier with the real actuator and feed a correctly-signed
    # RF_CONFIG envelope: the gate must verify it and hand the actuator the (type, payload), the
    # actuator must defer, and draining must apply exactly the config the signed envelope carried.
    from test_control_root_sink import ENV_RF_CONFIG_VALID, CONTROL_ROOT_HEX, TARGET_DEVICE_ID, _artifact
    from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
    from AlLoRa.DataSinks.DataSink import Reception

    node = _FakeNode()
    gate = Control_Root_DataSink(control_root=CONTROL_ROOT_HEX, device_id=TARGET_DEVICE_ID,
                                 actuator=Node_Control_Actuator(node))

    gate.consume(_artifact(tmp_path, ENV_RF_CONFIG_VALID), Reception(source="hub"))

    assert node.rf_calls == [], "the actuator defers: nothing switches during consume()"
    assert len(node.queued) == 1, "a verified RF_CONFIG must be queued through the gate->actuator path"

    node.queued[0]()
    assert node.rf_calls == [RF_PARSED], \
        "draining applies exactly the config carried by the signed envelope"

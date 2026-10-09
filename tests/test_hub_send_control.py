"""Unit - the Hub's verb for any signed command, and the one counter it shares with RF changes.

`send_control` is how a Hub that holds the control root sends RESET or CUSTOM. It numbers each
command from the same fleet-wide counter `ask_change_rf` uses. Two counters for one root would
overlap sooner or later, and the Edge would then refuse a genuine command as a replay, with
nothing on the Hub saying why.

What the Hub queues is checked the way the field checks it: by a real node's verify gate.
"""
import json

import pytest

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_types import CUSTOM, RESET, RF_CONFIG
from AlLoRa.DataSinks.DataSink import Reception
from test_control_root_sink import CONTROL_ROOT_PRIV, _CapturingActuator
from test_hub_ask_change_rf import _gate, _hub
from test_hub_endpoints import _make_hub, _node


def _rooted_hub(tmp_path):
    return _hub(tmp_path, control_root=Control_Root(CONTROL_ROOT_PRIV),
                control_counter_file=str(tmp_path / "control.counter"))


def test_a_custom_then_an_rf_change_are_both_accepted(tmp_path):
    hub, endpoint, source = _rooted_hub(tmp_path)
    actuator = _CapturingActuator(handles=(RF_CONFIG, CUSTOM))
    gate = _gate(actuator, Control_Root(CONTROL_ROOT_PRIV), tmp_path)

    hub.send_control(endpoint, CUSTOM, b"take a photo")
    hub.ask_change_rf(endpoint, {"sf": 9, "trial": 30})

    for queued in source.queued:
        gate.consume(queued, Reception(source="hub"))
    assert [t for t, _ in actuator.applied] == [CUSTOM, RF_CONFIG], \
        "the RF change must carry a number above the CUSTOM's, not be refused as a replay"
    assert actuator.applied[0][1] == b"take a photo"
    assert json.loads(actuator.applied[1][1].decode("utf-8"))["sf"] == 9


def test_send_control_queues_a_ctrl_file_and_reports_pending(tmp_path):
    hub, endpoint, source = _rooted_hub(tmp_path)
    outcome = hub.send_control(endpoint, RESET)
    assert outcome == hub.PENDING, "the Edge decides out of the Hub's sight, so it is pending"
    assert [f.get_name() for f in source.queued] == ["ctrl.bin"]


def test_an_unknown_type_is_refused_at_the_call(tmp_path):
    hub, endpoint, source = _rooted_hub(tmp_path)
    with pytest.raises(ValueError, match="control type"):
        hub.send_control(endpoint, 0x7F, b"")
    assert source.queued == [], "nothing may be queued for a refused command"


def test_an_rf_change_is_sent_with_ask_change_rf(tmp_path):
    # send_control does not move the Hub, so an RF change sent through it would leave the Hub
    # on the old radio settings, unable to hear the Edge it just moved.
    hub, endpoint, source = _rooted_hub(tmp_path)
    with pytest.raises(ValueError, match="ask_change_rf"):
        hub.send_control(endpoint, RF_CONFIG, b"{}")
    assert source.queued == []


def test_a_hub_without_a_root_cannot_send_control(tmp_path):
    hub, endpoint, source = _hub(tmp_path)
    with pytest.raises(ValueError, match="control root"):
        hub.send_control(endpoint, CUSTOM, b"take a photo")
    assert source.queued == []


def test_an_endpoint_without_a_device_id_cannot_be_addressed(tmp_path):
    hub = _make_hub(tmp_path, nodes=[_node("edge", "a1a1a1a1")],
                    control_root=Control_Root(CONTROL_ROOT_PRIV),
                    control_counter_file=str(tmp_path / "control.counter"))
    with pytest.raises(ValueError, match="device_id"):
        hub.send_control(hub.digital_endpoints[0], CUSTOM, b"take a photo")

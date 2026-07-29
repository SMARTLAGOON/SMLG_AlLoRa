"""The Gateway is the multi-endpoint deployment of the collector, so every tuning knob the
single-endpoint Hub accepts has to reach it too.

Its constructor used to name the forwarded arguments one by one, which silently dropped the
four visit-budget knobs added since: a fielded gateway was pinned to their defaults with no
way to override them short of assigning the attributes after construction. That matters most
for session_recovery_after, whose useful value is deployment-dependent (the detection window
is the budget times the visit cadence, and the cadence lives per endpoint in Nodes.json).
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Gateway import Gateway
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Nodes.Requester import Requester

GATEWAY_MAC = "b2b2b2b2"


def _write_config(path, result_path):
    config = {
        "name": "gw", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 9,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _write_nodes(path):
    nodes = [{"name": "edge", "mac_address": "a1a1a1a1", "active": True,
              "asking_frequency": 60, "listening_time": 30, "session_id": 42}]
    with open(path, "w") as f:
        json.dump(nodes, f)


def _make_gateway(tmp_path, **kwargs):
    config = str(tmp_path / "gw.json")
    nodes = str(tmp_path / "Nodes.json")
    _write_config(config, str(tmp_path / "results"))
    _write_nodes(nodes)
    return Gateway(Loopback_connector(GATEWAY_MAC), config_file=config, nodes_file=nodes, **kwargs)


def test_gateway_forwards_the_visit_budget_knobs(tmp_path):
    # The four budgets a Hub exposes must survive the Gateway constructor.
    gateway = _make_gateway(tmp_path, reclaim_timeout=4, probe_swap_after=2,
                            probe_give_up_after=9, session_recovery_after=5)

    assert gateway.reclaim_timeout == 4
    assert gateway.probe_swap_after == 2
    assert gateway.probe_give_up_after == 9
    assert gateway.session_recovery_after == 5


def test_gateway_keeps_the_hub_defaults_when_nothing_is_passed(tmp_path):
    # Forwarding must not change what an unconfigured gateway does.
    gateway = _make_gateway(tmp_path)
    reference = Hub(Loopback_connector(GATEWAY_MAC), config_file=str(tmp_path / "gw.json"))

    assert gateway.reclaim_timeout == reference.reclaim_timeout
    assert gateway.probe_swap_after == reference.probe_swap_after
    assert gateway.probe_give_up_after == reference.probe_give_up_after
    assert gateway.session_recovery_after == reference.session_recovery_after


def test_gateway_still_accepts_its_legacy_positional_arguments(tmp_path):
    # Fielded main.py files construct the gateway with the pre-v3 signature, including the
    # NEXT_ACTION_TIME_SLEEP the adaptive controller has ignored since v2.0.
    config = str(tmp_path / "gw.json")
    nodes = str(tmp_path / "Nodes.json")
    _write_config(config, str(tmp_path / "results"))
    _write_nodes(nodes)

    gateway = Gateway(Loopback_connector(GATEWAY_MAC), config, False, 0.1, nodes, None)

    assert len(gateway.digital_endpoints) == 1
    assert gateway.session_recovery_after == 3


def test_the_deprecated_requester_alias_forwards_the_same_knobs(tmp_path):
    # Requester is the pre-rename name for the same node; it already forwarded, and this pins
    # that the Gateway now matches it rather than diverging again.
    config = str(tmp_path / "gw.json")
    _write_config(config, str(tmp_path / "results"))

    requester = Requester(Loopback_connector(GATEWAY_MAC), config_file=config,
                          session_recovery_after=5)

    assert requester.session_recovery_after == 5

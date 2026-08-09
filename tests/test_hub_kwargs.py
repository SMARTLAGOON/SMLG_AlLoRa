"""Every tuning knob the Hub's constructor advertises has to reach the instance.

The multi-endpoint deployment used to be its own class, whose constructor named the
forwarded arguments one by one and so silently dropped the four visit-budget knobs added
since: a fielded gateway was pinned to their defaults with no way to override them short of
assigning the attributes after construction. That class is gone and the Hub is now the only
multi-endpoint node, which removes the forwarding seam but not the failure it allowed: the
knobs still have to land on the instance rather than be accepted and dropped.

That matters most for session_recovery_after, whose useful value is deployment-dependent
(the detection window is the budget times the visit cadence, and the cadence lives per
endpoint in Nodes.json).
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"


def _write_config(path, result_path):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False,
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


def _make_hub(tmp_path, **kwargs):
    config = str(tmp_path / "hub.json")
    nodes = str(tmp_path / "Nodes.json")
    _write_config(config, str(tmp_path / "results"))
    _write_nodes(nodes)
    return Hub(Loopback_connector(HUB_MAC), config_file=config, nodes_file=nodes, **kwargs)


def test_hub_lands_the_visit_budget_knobs_on_the_instance(tmp_path):
    hub = _make_hub(tmp_path, reclaim_timeout=4, probe_swap_after=2,
                    probe_give_up_after=9, session_recovery_after=5)

    assert hub.reclaim_timeout == 4
    assert hub.probe_swap_after == 2
    assert hub.probe_give_up_after == 9
    assert hub.session_recovery_after == 5


def test_a_multi_endpoint_hub_keeps_its_defaults_when_nothing_is_passed(tmp_path):
    # Registering endpoints must not disturb the budgets a single-endpoint Hub starts with.
    hub = _make_hub(tmp_path)
    reference = Hub(Loopback_connector(HUB_MAC), config_file=str(tmp_path / "hub.json"),
                    nodes_file=None)

    assert hub.reclaim_timeout == reference.reclaim_timeout
    assert hub.probe_swap_after == reference.probe_swap_after
    assert hub.probe_give_up_after == reference.probe_give_up_after
    assert hub.session_recovery_after == reference.session_recovery_after

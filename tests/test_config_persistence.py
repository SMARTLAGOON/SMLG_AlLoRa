"""Config persistence: backup_config must round-trip losslessly and reflect the LIVE RF
config, so a committed RF-config trial (and a v3/secure posture) survives a reboot.

Two defects this pins:
  1. Node.backup_config rebuilt a hand-picked subset, dropping protocol_version /
     security_mode / session_id / result_path / identity_file: a secure v3 node came back as
     an open v2 node off its own network.
  2. The connector block came from connector.backup_config(), which returned the STALE
     config_parameters dict: a change_rf_config (the committed trial) never reached it, so
     the node rebooted on the OLD radio config.

A bridge board persists nothing at runtime, so it has no backup_config; what matters for one
is that it reads its config the same way a node does. Seam B covers that.
"""
import json

from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge


def _connector_config():
    return {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "min_timeout": 0.5, "max_timeout": 12, "debug": False}


# --- Seam C: the connector reports its LIVE RF config, under the canonical LoRa.json keys --

def test_connector_backup_reflects_the_live_rf_config():
    conn = Loopback_connector("a1a1a1a1")
    conn.config(_connector_config())
    # A committed trial moved the radio off the configured values.
    conn.change_rf_config(frequency=915, sf=9, bw=250, cr=2, tx_power=20)

    backup = conn.backup_config()

    assert backup["sf"] == 9
    assert backup["freq"] == 915
    assert backup["bandwidth"] == 250
    assert backup["coding_rate"] == 2
    assert backup["tx_power"] == 20
    # Non-RF connector keys are preserved verbatim.
    assert backup["min_timeout"] == 0.5
    assert backup["max_timeout"] == 12


# --- Seam A: Node.backup_config round-trips the whole LoRa.json ----------------------------

def _write_v3_config(path, extra_top=None):
    config = {
        "name": "S", "chunk_size": 200, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 42,
        "result_path": "Results", "debug": False,
        "connector": _connector_config(),
    }
    if extra_top:
        config.update(extra_top)
    with open(path, "w") as f:
        json.dump(config, f)
    return config


def test_node_backup_config_preserves_the_v3_posture_and_persists_the_live_rf(tmp_path):
    path = str(tmp_path / "LoRa.json")
    _write_v3_config(path, extra_top={"custom_field": "keep-me"})

    node = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    node.change_rf_config({"sf": 9})     # a committed trial moves sf 7 -> 9
    node.backup_config()

    reloaded = json.load(open(path))
    # (a) the v3 / secure / identity posture and every other top-level key survive.
    assert reloaded["protocol_version"] == 3
    assert reloaded["security_mode"] == "open"
    assert reloaded["session_id"] == 42
    assert reloaded["result_path"] == "Results"
    assert reloaded["short_mac"] is True
    assert reloaded["custom_field"] == "keep-me"
    # (b) the connector block reflects the live RF (sf 9), other connector keys intact.
    assert reloaded["connector"]["sf"] == 9
    assert reloaded["connector"]["min_timeout"] == 0.5


def test_a_node_rebuilt_from_the_backup_still_boots_as_v3(tmp_path):
    # The whole point: a reconfig + reboot must not silently downgrade a secure v3 node to
    # an open v2 node off its own network.
    path = str(tmp_path / "LoRa.json")
    _write_v3_config(path)

    Edge(Loopback_connector("a1a1a1a1"), config_file=path).backup_config()

    rebuilt = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    assert rebuilt.protocol_version == 3
    assert rebuilt.session_id == 42


# --- Seam B: a bridge reads the same LoRa.json, and never writes to it ---------------------

def test_an_adapter_reads_its_v3_posture_and_link_block_without_persisting(tmp_path):
    path = str(tmp_path / "LoRa.json")
    config = {
        "name": "T", "chunk_size": 235, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 7, "debug": False,
        "connector": _connector_config(),
        "adapter": {"uartid": 0, "baud": 9600},
    }
    with open(path, "w") as f:
        json.dump(config, f)

    seen = {}

    class _Probe(Adapter):
        def setup_link(self, cfg):
            seen.update(cfg)

    radio = Loopback_connector("c3c3c3c3")
    _Probe(radio, config_file=path)

    # The v3 posture reaches the radio, and the link block reaches the medium.
    assert radio.protocol_version == 3 and radio.addressing == "sid"
    assert seen == {"uartid": 0, "baud": 9600}
    # And the file is untouched: a bridge holds no state worth persisting, so the whole
    # round-trip hazard above simply does not apply to it.
    assert json.load(open(path)) == config

"""A secure node with no crypto backend refuses to run.

The dangerous failure is a *silent* one: a node configured secure that quietly runs plaintext
because its firmware lacks the AEAD backend is worse than one that stops — the operator thinks
their traffic is protected. So a secure/strict node with no backend halts loudly. The escape
hatch is an explicit, opt-in allow_insecure_fallback for tests/bring-up, never the default.
"""
import json

import pytest

import AlLoRa.Security.AEAD as AEAD
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _write(path, **overrides):
    config = {
        "name": "s", "chunk_size": 200, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "secure", "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config.update(overrides)
    with open(path, "w") as f:
        json.dump(config, f)


def _make_source(tmp_path, **overrides):
    cfg = str(tmp_path / "LoRa.json")
    _write(cfg, **overrides)
    conn, _ = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    return Edge(conn, config_file=cfg)


def test_secure_node_refuses_to_run_without_a_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(AEAD, "detect_aead", lambda: None)   # simulate firmware with no AEAD
    with pytest.raises(Exception):
        _make_source(tmp_path)   # secure + no backend + no opt-in -> halt


def test_secure_node_runs_open_only_with_the_explicit_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(AEAD, "detect_aead", lambda: None)
    source = _make_source(tmp_path, allow_insecure_fallback=True)   # loud, opt-in degrade
    assert source.aead is None                                      # ran open, did not seal


def test_secure_node_runs_secure_when_the_backend_is_present(tmp_path):
    # the real CPython backend is available -> no refusal, secure is live
    source = _make_source(tmp_path)
    assert source.aead is not None

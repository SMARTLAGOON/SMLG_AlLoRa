"""Unit — the 1-byte session id is identity-derived.

v2 (and the first v3 build) made the operator assign each session a sid by hand. v3 secure
derives it from the node's identity instead — device_id[0] in secure, the device-specific low
byte of the short MAC in open — so registering a node is one value (its device_id / MAC), not
two. An explicit session_id in config still overrides (debugging, or the Collector's on-clash
reassignment).
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"


def _write(path, **overrides):
    config = {
        "name": "s", "chunk_size": 200, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config.update(overrides)
    with open(path, "w") as f:
        json.dump(config, f)


def _source(tmp_path, **overrides):
    cfg = str(tmp_path / "LoRa.json")
    _write(cfg, **overrides)
    conn, _ = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    return Source(conn, config_file=cfg)


def test_secure_source_derives_sid_from_its_device_id(tmp_path):
    # no session_id in config -> the sid is the first byte of device_id = SHA256(identity key)
    source = _source(tmp_path, security_mode="secure")
    assert source.device_id is not None
    assert source.session_id == source.device_id[0]


def test_explicit_session_id_overrides_the_derived_sid(tmp_path):
    source = _source(tmp_path, security_mode="secure", session_id=42)
    assert source.session_id == 42


def test_open_source_derives_sid_from_the_short_mac_low_byte(tmp_path):
    # open mode has no device_id; the sid is the device-specific low byte of the short MAC
    # (a1a1a1a1 -> 0xa1), never the vendor-OUI high bytes.
    source = _source(tmp_path, security_mode="open")
    assert source.session_id == int(SOURCE_MAC[-2:], 16)


def test_mac_registered_endpoint_derives_the_same_open_sid_as_the_source():
    # The Collector's view must match: a MAC-registered endpoint with no explicit sid derives
    # the same short-MAC low byte, so an open v3 deployment needs no hand-assigned session_id.
    from AlLoRa.Digital_Endpoint import Digital_Endpoint
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC)
    assert endpoint.session_id == int(SOURCE_MAC[-2:], 16)

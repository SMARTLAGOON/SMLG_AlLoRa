"""The fleet directory: where a deployment's authority lives, and what it remembers.

The wizard's whole security posture rests on this one object. It holds the control root's
private half, the mint counter that stops a second signer replaying the first's numbers, and
the registry of `device_id`s the operator has issued. Three rules it must never break:

  * a root is never replaced, because every node is pinned to the one it was given;
  * the counter travels with the root, keyed by that root's fingerprint exactly as the Hub
    keys its own, so a handover moves both files or neither;
  * the private half is never what gets staged for a board that only verifies.
"""
import json
import os

import pytest

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Security.ec_p256 import public_key_uncompressed
from tools.allora_provision.fleet import (
    CONTROL_ROOT_NAME, Fleet, PRIVATE_HEX_LEN, PUBLIC_HEX_LEN, classify_root_half)

# RFC 6979 A.2.5's published test scalar, used here because it is unmistakably not a real key.
ROOT_PRIV_HEX = "c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721"
ROOT_PUB_HEX = public_key_uncompressed(int(ROOT_PRIV_HEX, 16)).hex()
DEVICE_ID = bytes(range(32)).hex()
OTHER_DEVICE_ID = bytes(range(32, 64)).hex()


def _fleet(tmp_path):
    return Fleet(str(tmp_path / "allora-fleet"))


# --- the root ---------------------------------------------------------------------------

def test_creating_a_root_writes_the_private_half_and_reports_it_as_new(tmp_path):
    fleet = _fleet(tmp_path)
    assert not fleet.has_root()
    root, created = fleet.load_or_create_root()
    assert created is True
    assert isinstance(root, Control_Root)
    with open(fleet.root_key_path) as f:
        material = f.read().strip()
    assert len(material) == PRIVATE_HEX_LEN
    assert fleet.has_root()


def test_a_second_call_keeps_the_existing_root(tmp_path):
    """Every node is pinned to the root it was given, so a fresh one locks the fleet out of
    its own control plane. Adding a node later must be safe."""
    fleet = _fleet(tmp_path)
    first, _ = fleet.load_or_create_root()
    second, created = fleet.load_or_create_root()
    assert created is False
    assert second.fingerprint() == first.fingerprint()
    assert second.public_key_hex() == first.public_key_hex()


def test_the_public_half_is_what_gets_staged_for_a_commanded_node(tmp_path):
    """The halves are told apart by length and the wrong one halts the node. A staged file
    for an Edge carries 130 hex characters, and it is named the way the config line names it."""
    fleet = _fleet(tmp_path)
    fleet.load_or_create_root()
    staged = fleet.stage_public_root()
    assert os.path.basename(staged) == CONTROL_ROOT_NAME
    with open(staged) as f:
        material = f.read().strip()
    assert len(material) == PUBLIC_HEX_LEN
    assert classify_root_half(material) == "public"


def test_the_private_half_is_staged_only_for_the_on_site_root_mode(tmp_path):
    fleet = _fleet(tmp_path)
    fleet.load_or_create_root()
    staged = fleet.stage_private_root()
    with open(staged) as f:
        assert len(f.read().strip()) == PRIVATE_HEX_LEN
    assert staged != fleet.stage_public_root()


def test_classify_refuses_material_that_is_neither_half(tmp_path):
    assert classify_root_half(ROOT_PRIV_HEX) == "private"
    assert classify_root_half(ROOT_PUB_HEX) == "public"
    with pytest.raises(ValueError):
        classify_root_half("deadbeef")


def test_a_root_file_that_holds_the_public_half_is_refused_as_a_fleet_root(tmp_path):
    """The fleet directory is the authority's home. A public key there means somebody copied
    the wrong half back, and the wizard would then quietly mint nothing."""
    fleet = _fleet(tmp_path)
    fleet.ensure()
    with open(fleet.root_key_path, "w") as f:
        f.write(ROOT_PUB_HEX)
    with pytest.raises(ValueError) as excinfo:
        fleet.load_or_create_root()
    assert "public" in str(excinfo.value)


# --- the counter ------------------------------------------------------------------------

def test_the_counter_starts_at_zero_and_is_keyed_to_its_root(tmp_path):
    fleet = _fleet(tmp_path)
    root, _ = fleet.load_or_create_root()
    assert fleet.counter() == 0
    assert fleet.next_counter() == 1
    assert fleet.next_counter() == 2
    with open(fleet.counter_path) as f:
        mark = json.load(f)
    assert mark == {"root": root.fingerprint().hex(), "counter": 2}


def test_a_counter_left_by_another_root_does_not_count_for_this_one(tmp_path):
    """The Hub reads its mark the same way. Rotating the root is the one reset for the whole
    control plane, on both ends."""
    fleet = _fleet(tmp_path)
    fleet.load_or_create_root()
    with open(fleet.counter_path, "w") as f:
        json.dump({"root": "00" * 32, "counter": 99}, f)
    assert fleet.counter() == 0
    assert fleet.next_counter() == 1


def test_the_counter_survives_a_reopened_fleet(tmp_path):
    fleet = _fleet(tmp_path)
    fleet.load_or_create_root()
    fleet.next_counter()
    fleet.next_counter()
    reopened = _fleet(tmp_path)
    reopened.load_or_create_root()
    assert reopened.counter() == 2
    assert reopened.next_counter() == 3


# --- the registry -----------------------------------------------------------------------

def test_registering_a_node_records_it_and_survives_a_reopen(tmp_path):
    fleet = _fleet(tmp_path)
    fleet.register(name="S", role="edge", device_id=DEVICE_ID, posture="secure")
    entries = _fleet(tmp_path).entries()
    assert len(entries) == 1
    assert entries[0]["name"] == "S"
    assert entries[0]["device_id"] == DEVICE_ID
    assert entries[0]["role"] == "edge"
    assert entries[0]["active"] is True


def test_registering_the_same_device_id_twice_updates_rather_than_duplicates(tmp_path):
    """A reflashed board keeps its device_id when its identity was restored. Re-provisioning
    it must not leave the Hub polling the same node twice."""
    fleet = _fleet(tmp_path)
    fleet.register(name="S", role="edge", device_id=DEVICE_ID, posture="secure")
    fleet.register(name="S2", role="edge", device_id=DEVICE_ID, posture="control")
    entries = fleet.entries()
    assert len(entries) == 1
    assert entries[0]["name"] == "S2"
    assert entries[0]["posture"] == "control"


def test_the_hub_roster_carries_only_the_edges_and_only_the_keys_nodes_json_names(tmp_path):
    fleet = _fleet(tmp_path)
    fleet.register(name="S", role="edge", device_id=DEVICE_ID, posture="secure")
    fleet.register(name="R", role="hub", device_id=OTHER_DEVICE_ID, posture="secure")
    roster = json.loads(fleet.render_nodes_json())
    assert [entry["name"] for entry in roster] == ["S"]
    assert roster[0]["device_id"] == DEVICE_ID
    assert "posture" not in roster[0]
    assert "identity_backup" not in roster[0]


def test_the_rendered_roster_is_what_the_hub_reads_back(tmp_path):
    """The point of the file is that `Hub.add_digital_endpoints` accepts it unedited."""
    from AlLoRa.Connectors.Loopback_connector import Loopback_connector
    from AlLoRa.Nodes.Hub import Hub

    fleet = _fleet(tmp_path)
    fleet.register(name="S", role="edge", device_id=DEVICE_ID, posture="secure")
    roster_path = str(tmp_path / "Nodes.json")
    with open(roster_path, "w") as f:
        f.write(fleet.render_nodes_json())
    config_path = str(tmp_path / "LoRa.json")
    with open(config_path, "w") as f:
        json.dump({"name": "R", "protocol_version": 3, "security_mode": "open",
                   "result_path": str(tmp_path / "Results"),
                   "connector": {"sf": 7, "freq": 868, "bandwidth": 125,
                                 "coding_rate": 1, "tx_power": 14, "debug": False}}, f)

    hub = Hub(Loopback_connector("b2b2b2b2"), config_file=config_path, nodes_file=roster_path)
    assert len(hub.digital_endpoints) == 1
    assert hub.digital_endpoints[0].device_id.hex() == DEVICE_ID


# --- the identity backup ----------------------------------------------------------------

def test_an_identity_backup_is_kept_under_the_fleet_and_never_overwritten(tmp_path):
    """The flash erases the board filesystem and the device_id is derived from that key, so a
    backup that a second run silently replaced would be no backup at all."""
    fleet = _fleet(tmp_path)
    first = fleet.backup_identity("d909f4eb", "aa" * 32)
    second = fleet.backup_identity("d909f4eb", "bb" * 32)
    assert first != second
    with open(first) as f:
        assert f.read().strip() == "aa" * 32
    with open(second) as f:
        assert f.read().strip() == "bb" * 32


def test_a_backed_up_identity_reproduces_the_device_id_the_board_computes(tmp_path):
    """This is the check that makes a backup worth taking: the same scalar run through the
    library's own crypto on the laptop yields the fingerprint the operator registered."""
    from AlLoRa.Security.identity import device_id_from_pubkey

    fleet = _fleet(tmp_path)
    path = fleet.backup_identity("abcd1234", ROOT_PRIV_HEX)
    expected = device_id_from_pubkey(public_key_uncompressed(int(ROOT_PRIV_HEX, 16))).hex()
    assert fleet.device_id_of_identity_file(path) == expected

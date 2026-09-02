"""What the wizard writes onto a board: `LoRa.json`, per placement and per posture.

The posture is not a filename and not a flag the node discovers; it is what the config says,
and every rule below exists because getting one of them wrong produces a board that looks
provisioned and is not. Three the tests pin down:

  * an open node has no identity to be addressed by, so it needs a hand-assigned `session_id`
    that matches its peer, and a secure one must not carry one at all (the sid derives from
    `device_id[0]`, and an override would fight it);
  * `identity_file` is what makes a node generate and keep an identity, so a secure config
    without it comes up with no `device_id`;
  * a control root on a node whose config still says open is the half-provisioned state the
    example halts on, and the wizard must never write it.
"""
import json

import pytest

from tools.allora_provision.node_config import (
    DEFAULT_RF, RF_FIELDS, build_lora_json, merge_rf)


def test_a_secure_edge_names_its_identity_file_and_no_session_id():
    config = build_lora_json(role="edge", posture="secure")
    assert config["security_mode"] == "secure"
    assert config["identity_file"] == "identity.key"
    assert config["protocol_version"] == 3
    assert "session_id" not in config
    assert "control_root_file" not in config


def test_an_open_node_carries_the_session_id_that_addresses_it():
    config = build_lora_json(role="edge", posture="open", session_id=42)
    assert config["security_mode"] == "open"
    assert config["session_id"] == 42
    assert "identity_file" not in config


def test_an_open_node_without_a_session_id_is_refused():
    """v3 open data transfer is sid-addressed and the two ends must agree. Defaulting the
    value would produce a pair that cannot hear each other and no line saying why."""
    with pytest.raises(ValueError):
        build_lora_json(role="edge", posture="open")


def test_the_control_posture_pins_the_root_on_the_node_that_verifies():
    config = build_lora_json(role="edge", posture="control")
    assert config["security_mode"] == "secure"
    assert config["identity_file"] == "identity.key"
    assert config["control_root_file"] == "control_root.key"


def test_the_hub_gets_no_root_under_the_default_offline_mode():
    """The control root lives with the operator. The Hub is a courier: signed artifacts are
    minted elsewhere and handed to `ask_change_rf`, so nothing on the board names a root."""
    config = build_lora_json(role="hub", posture="control")
    assert "control_root_file" not in config
    assert config["security_mode"] == "secure"


def test_the_hub_holds_the_root_only_in_the_named_on_site_mode():
    config = build_lora_json(role="hub", posture="control", on_site_root=True)
    assert config["control_root_file"] == "control_root.key"


def test_on_site_root_is_meaningless_without_the_control_posture():
    with pytest.raises(ValueError):
        build_lora_json(role="hub", posture="secure", on_site_root=True)


def test_an_edge_is_never_given_the_on_site_root_mode():
    """That mode puts the *private* half on the machine running Hub logic. An Edge holding it
    is the fleet's signing key on a field node, which the node itself refuses to boot with."""
    with pytest.raises(ValueError):
        build_lora_json(role="edge", posture="control", on_site_root=True)


def test_only_the_hub_names_where_received_files_land():
    assert build_lora_json(role="hub", posture="secure")["result_path"] == "Results"
    assert "result_path" not in build_lora_json(role="edge", posture="secure")


def test_the_radio_block_defaults_and_takes_overrides():
    config = build_lora_json(role="edge", posture="secure", rf={"sf": 9, "tx_power": 20})
    assert config["connector"]["sf"] == 9
    assert config["connector"]["tx_power"] == 20
    assert config["connector"]["freq"] == DEFAULT_RF["freq"]
    assert config["connector"]["bandwidth"] == DEFAULT_RF["bandwidth"]


def test_merge_rf_refuses_a_setting_the_radio_does_not_have():
    """A typo in an RF flag would otherwise be written to the board and ignored, leaving the
    pair on settings the operator believes they changed."""
    assert merge_rf({"sf": 9})["sf"] == 9
    with pytest.raises(ValueError) as excinfo:
        merge_rf({"spreading_factor": 9})
    assert "spreading_factor" in str(excinfo.value)


def test_the_pair_a_wizard_writes_matches_on_every_setting_that_has_to_match():
    """`sf`, `freq`, `bandwidth` and `coding_rate` must agree across the two boards or they do
    not hear each other. One RF block feeds both configs, which is what makes that true."""
    rf = {"sf": 10, "freq": 867, "bandwidth": 250, "coding_rate": 2}
    edge = build_lora_json(role="edge", posture="secure", rf=rf)
    hub = build_lora_json(role="hub", posture="secure", rf=rf)
    for field in ("sf", "freq", "bandwidth", "coding_rate"):
        assert edge["connector"][field] == hub["connector"][field]


def test_the_written_config_is_json_a_node_can_read():
    config = build_lora_json(role="hub", posture="control")
    assert json.loads(json.dumps(config)) == config
    assert set(RF_FIELDS) <= set(config["connector"])


def test_the_two_placements_get_different_default_names():
    assert build_lora_json(role="edge", posture="secure")["name"] == "S"
    assert build_lora_json(role="hub", posture="secure")["name"] == "R"
    assert build_lora_json(role="edge", posture="secure", name="rooftop")["name"] == "rooftop"


def test_an_unknown_placement_or_posture_is_refused():
    with pytest.raises(ValueError):
        build_lora_json(role="gateway", posture="secure")
    with pytest.raises(ValueError):
        build_lora_json(role="edge", posture="strict")


def test_a_sx1262_config_carries_the_pins_its_board_wires_the_radio_on():
    """The connector reads its pins out of the connector block and falls back to a different
    board's map when they are absent, so a config that names none provisions a board that
    cannot find its own chip and says nothing about it."""
    connector = build_lora_json(role="edge", posture="secure", driver="sx1262")["connector"]
    assert connector["clk"] == 5
    assert connector["mosi"] == 6
    assert connector["miso"] == 3
    assert connector["cs"] == 7
    assert connector["rst"] == 8
    assert connector["irq"] == 33
    assert connector["gpio"] == 34


def test_a_sx127x_config_carries_no_pins_at_all():
    """That driver takes its pins from its own board file. A pin written here would be a number
    in the operator's config that nothing reads, which is the same defect from the other end."""
    connector = build_lora_json(role="edge", posture="secure", driver="sx127x")["connector"]
    for pin in ("clk", "mosi", "miso", "cs", "rst", "irq", "gpio"):
        assert pin not in connector


def test_both_ends_of_a_mixed_pair_get_their_own_wiring():
    """The bench pair is an SX127x Hub and an SX1262 Edge on the same board model. One radio is
    wired through the config and the other through its driver, and each has to get its own."""
    edge = build_lora_json(role="edge", posture="secure", driver="sx1262")["connector"]
    hub = build_lora_json(role="hub", posture="secure", driver="sx127x")["connector"]
    assert edge["cs"] == 7
    assert "cs" not in hub


def test_a_board_this_toolkit_has_no_pin_map_for_is_refused():
    """Guessing here writes a config that looks provisioned and cannot boot, which is exactly
    the failure the missing pins produced."""
    with pytest.raises(ValueError) as excinfo:
        build_lora_json(role="edge", posture="secure", driver="sx1262", board="heltec_v3")
    assert "heltec_v3" in str(excinfo.value)
    assert "t3s3" in str(excinfo.value)


def test_the_config_names_no_device_section():
    """A `device.board` key makes the node build a board object, and on a board whose screen
    the file does not match that raises before any radio code runs. The wizard provisions the
    radio and leaves peripherals to a config an operator writes deliberately."""
    assert "device" not in build_lora_json(role="edge", posture="secure", driver="sx1262")

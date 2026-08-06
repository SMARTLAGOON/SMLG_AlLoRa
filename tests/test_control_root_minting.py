"""Unit — the minting half of the control root.

Until now the library could only ever verify a control artifact: something outside it had to
mint one, which is why the control path had never run end to end. `Control_Root` is the
counterpart to `Control_Root_DataSink`, so a deployment can hold its own authority. A Hub
given the root private key mints locally; a Hub given none carries what a backend minted, and
both reach the same node.

The tests here compose the two halves: what this mints, the real gate must accept, and what
it must not accept is just as much the point.
"""
import os

import pytest

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_types import RF_CONFIG, RESET
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.DataSinks.DataSink import Reception
from test_control_root_sink import (
    CONTROL_ROOT_PRIV, OTHER_ROOT_PRIV, TARGET_DEVICE_ID, OTHER_DEVICE_ID, RF_PAYLOAD,
    _CapturingActuator, _artifact,
)


def _gate(actuator, root, device_id=TARGET_DEVICE_ID, counter_file=None):
    return Control_Root_DataSink(control_root=root.public_key(), device_id=device_id,
                                 actuator=actuator, counter_file=counter_file)


def test_a_minted_artifact_is_accepted_by_the_gate_it_was_minted_for(tmp_path):
    # The end-to-end the control path has never had: mint here, verify there, actuate.
    root = Control_Root(CONTROL_ROOT_PRIV)
    actuator = _CapturingActuator()
    gate = _gate(actuator, root)

    artifact = root.mint(RF_CONFIG, TARGET_DEVICE_ID, RF_PAYLOAD, counter=1)
    gate.consume(_artifact(tmp_path, artifact), Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], \
        "an artifact minted by the root a node trusts must reach that node's actuator"


def test_an_artifact_from_another_root_is_refused(tmp_path):
    # What pinning is for. Another authority's key is perfectly valid ECDSA; it just has no
    # standing with this node, and a signature that verifies against the wrong root is exactly
    # what an attacker with their own keypair would produce.
    stranger = Control_Root(OTHER_ROOT_PRIV)
    actuator = _CapturingActuator()
    gate = _gate(actuator, Control_Root(CONTROL_ROOT_PRIV))

    gate.consume(_artifact(tmp_path, stranger.mint(RF_CONFIG, TARGET_DEVICE_ID, RF_PAYLOAD)),
                 Reception(source="hub"))

    assert actuator.applied == [], "a node must act only for the root it was provisioned with"


def test_an_artifact_minted_for_another_node_is_refused(tmp_path):
    # One root commands a whole fleet, so a genuine command for a sibling node is a real
    # artifact with a real signature. Only the target binding keeps it off this node.
    root = Control_Root(CONTROL_ROOT_PRIV)
    actuator = _CapturingActuator()
    gate = _gate(actuator, root)

    gate.consume(_artifact(tmp_path, root.mint(RF_CONFIG, OTHER_DEVICE_ID, RF_PAYLOAD)),
                 Reception(source="hub"))

    assert actuator.applied == [], "a command addressed to a sibling must not execute here"


def test_a_generated_root_can_command_a_node_provisioned_with_its_public_half(tmp_path):
    # The no-backend deployment: a Hub makes its own authority, the operator copies the public
    # half onto the nodes, and the fleet can be reconfigured with nothing else involved.
    root = Control_Root.generate(os.urandom)
    actuator = _CapturingActuator()
    gate = _gate(actuator, root)

    gate.consume(_artifact(tmp_path, root.mint(RESET, TARGET_DEVICE_ID, b"")),
                 Reception(source="hub"))

    assert actuator.applied == [(RESET, b"")]
    assert len(root.public_key()) == 65 and root.public_key()[0] == 0x04
    assert root.public_key_hex() == root.public_key().hex()


def test_two_generated_roots_are_different_authorities():
    # A keygen that returned the same key twice would give every deployment the same root.
    assert Control_Root.generate(os.urandom).public_key() != \
        Control_Root.generate(os.urandom).public_key()


def test_a_root_loads_from_the_hex_a_key_file_holds():
    # A key file carries hex, so the constructor takes hex as readily as the scalar; both must
    # name the same authority.
    from_scalar = Control_Root(CONTROL_ROOT_PRIV)
    from_hex = Control_Root("{:064x}".format(CONTROL_ROOT_PRIV))

    assert from_hex.public_key() == from_scalar.public_key()
    assert from_hex.fingerprint() == from_scalar.fingerprint()


def test_a_key_that_is_not_a_usable_scalar_is_refused_at_construction():
    # A mis-provisioned root must fail where the operator can see it, not later as a stream of
    # artifacts every node rejects.
    for bad in ("not-hex", "", 0, -1):
        with pytest.raises(ValueError):
            Control_Root(bad)


def test_minting_refuses_arguments_a_node_could_never_accept():
    # Each of these would mint bytes no gate can act on, so they are caller mistakes worth
    # catching at the mint rather than debugging as a silent non-delivery over a radio link.
    root = Control_Root(CONTROL_ROOT_PRIV)
    with pytest.raises(ValueError):
        root.mint(RF_CONFIG, b"\x00\x01\x02\x03", RF_PAYLOAD)          # not a 32-byte target
    with pytest.raises(ValueError):
        root.mint(RF_CONFIG, TARGET_DEVICE_ID, RF_PAYLOAD, counter=0)  # below the first counter
    with pytest.raises(ValueError):
        root.mint(0x1FF, TARGET_DEVICE_ID, RF_PAYLOAD)                 # not a single byte


def test_a_minted_sequence_keeps_working_as_the_counter_advances(tmp_path):
    # The operator's normal life: repeated reconfiguration. Each artifact must land, and the
    # counter the minter chose is what lets the node tell the next command from a replay.
    root = Control_Root(CONTROL_ROOT_PRIV)
    actuator = _CapturingActuator()
    gate = _gate(actuator, root)

    for counter in (1, 2, 3):
        gate.consume(
            _artifact(tmp_path, root.mint(RF_CONFIG, TARGET_DEVICE_ID, RF_PAYLOAD,
                                          counter=counter), name="c{}.bin".format(counter)),
            Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)] * 3

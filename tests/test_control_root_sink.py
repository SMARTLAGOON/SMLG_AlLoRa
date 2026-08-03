"""Unit — the control-root verify layer (the Edge-side downlink gate).

A downlink control artifact (RF config change, reset, later a model/OTA) is authenticated
against a pinned per-deployment control-root public key *before* the Edge acts on it. The
signed artifact is ordinary file content carried by the reversed-pull transfer; the wire
(the frozen v3 packet) is untouched. This layer sits on the Edge's downlink DataSink,
operates on the fully reassembled artifact, and is the gate, not the actuator: on a verified
artifact it hands the wrapped actuator the trusted (control_type, payload); on anything
that fails it drops the artifact and never forwards.

Envelope:  version(1) || type(1) || target_device_id(32) || payload || sig(64)
           sig = ECDSA-P256 over SHA-256( version || type || target || payload ), raw r||s.

Vectors below are frozen literals authored with the `ecdsa` lib and cross-checked against this
project's own ec_p256.ecdsa_verify (see the scratchpad generator), so the suite stays
dependency-free and the runtime only ever verifies, never signs.
"""
import math
import os

import pytest

from AlLoRa.File import AlLoRa_File
from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.Control.Control_Actuator import Control_Actuator
from AlLoRa.Control.control_types import RF_CONFIG, RESET, MODEL, OTA
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink, ENVELOPE_VERSION

# --- frozen vectors (authored with ecdsa 0.18.0; see scratchpad/gen_vectors.py) -------------

CONTROL_ROOT_PUB = bytes.fromhex(
    "04e184ae8152166cbf2ed1a6647627d0d3e6d2c806e79e383865e67ac14273ce8e"
    "5f2549640fd707f6347253a2e0959d572ee8298dc1cf1f90130fc3097fe8fb7c"
)
TARGET_DEVICE_ID = bytes.fromhex(
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
OTHER_DEVICE_ID = bytes.fromhex(
    "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
)
RF_PAYLOAD = b'{"sf":9,"bw":125,"tx_power":14}'

# version=1 type=RF_CONFIG target=TARGET payload=RF_PAYLOAD, correctly signed by the control root.
ENV_RF_CONFIG_VALID = bytes.fromhex(
    "0101000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "7b227366223a392c226277223a3132352c2274785f706f776572223a31347d"
    "77b659a479d20d850078cfd8cc6eaad868882177ccdb4b8e3be6207fcbdacb43"
    "f276ce4f98dfb9270f2c6b7783c3d987aa1848693b594f55cbec2809f15b8574"
)
# version=1 type=RESET target=TARGET payload=b"" (empty), correctly signed.
ENV_RESET_VALID = bytes.fromhex(
    "0102000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "3a17bd32dbef93f3e0c72ae9860f37b2ecc2cc0e2990d6637dbf1414112568bc"
    "e2933245fc971a587ffaaf6050b1ea24c0946906bfa31d2fdf36ff676a84b2e4"
)
# validly signed but type=0x7f (undefined / no actuator) — must still be dropped.
ENV_UNKNOWN_TYPE_VALID = bytes.fromhex(
    "017f000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "7b227366223a392c226277223a3132352c2274785f706f776572223a31347d"
    "a32bb522a362af8263edbcffeadaa32b50a1a9790b424bab4d23ddaa5f86123a"
    "26bf93cc6d22d065f219a9ef5c4a47081b6a00d654c7749f26fac27c54079fc6"
)
# validly signed but version=2 (unsupported) — must be dropped (cross-version confusion).
ENV_BAD_VERSION_VALID = bytes.fromhex(
    "0201000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
    "7b227366223a392c226277223a3132352c2274785f706f776572223a31347d"
    "dda38dad9766a28fdf4241393e42c3bebda263890e6552337205482b27154552"
    "ee67cc432814201ca1bc9c258f51d18d910e6625ba0c7e1a3f03d38540c52108"
)

CONTROL_ROOT_HEX = CONTROL_ROOT_PUB.hex()


class _CapturingActuator(Control_Actuator):
    """The v3.0.0 consumer: no real actuator yet, it just records what it was handed. In
    production this is where change_rf_config / reset / OTA live; that is a later increment."""

    def __init__(self):
        self.applied = []   # (control_type, payload)

    def apply(self, control_type, payload):
        self.applied.append((control_type, bytes(payload)))


def _artifact(tmp_path, content, name="ctrl.bin", chunk_size=32):
    """A fully-reassembled received-style AlLoRa_File carrying `content` as its bytes, exactly
    as the Edge holds a pulled downlink artifact the instant before its sink sees it."""
    num_chunks = max(1, math.ceil(len(content) / chunk_size))
    f = AlLoRa_File(name=name, length=num_chunks, chunk_size=chunk_size, path=str(tmp_path))
    for i in range(num_chunks):
        f.add_chunk(i, content[i * chunk_size:(i + 1) * chunk_size])
    return f


def _sink(actuator, device_id=TARGET_DEVICE_ID, control_root=CONTROL_ROOT_HEX):
    return Control_Root_DataSink(control_root=control_root, device_id=device_id,
                                 actuator=actuator)


# --- Slice 1: a verified artifact is handed to the actuator ---------------------------

def test_valid_rf_config_artifact_is_delivered_to_the_actuator(tmp_path):
    actuator = _CapturingActuator()
    sink = _sink(actuator)
    sink.consume(_artifact(tmp_path, ENV_RF_CONFIG_VALID), Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], \
        "a genuine control artifact must reach the actuator as (type, payload)"


# --- Slice 2: a forged signature is dropped, never forwarded, and never raises --------------

def test_forged_signature_is_dropped_and_does_not_raise(tmp_path):
    forged = bytearray(ENV_RF_CONFIG_VALID)
    forged[-1] ^= 0x01   # flip one bit of s: still a structurally valid 64-B sig, just not genuine
    actuator = _CapturingActuator()
    sink = _sink(actuator)

    # Must NOT raise: raising re-pulls the identical bytes forever and withholds the final-OK.
    # A rejected artifact is consumed-once (transfer completes) and simply never acted upon.
    sink.consume(_artifact(tmp_path, bytes(forged)), Reception(source="hub"))

    assert actuator.applied == [], "a forged control artifact must never reach the actuator"


# --- Slice 3: a genuine artifact addressed to another node is dropped (target-binding) ------

def test_artifact_targeting_another_node_is_dropped(tmp_path):
    actuator = _CapturingActuator()
    # ENV_RF_CONFIG_VALID is a genuine, correctly-signed command for TARGET_DEVICE_ID. Delivered
    # to a node whose own device_id is OTHER, it must not execute: a fleet-wide control root makes
    # a real command replayable to another node, and the target check is what blocks retargeting.
    sink = _sink(actuator, device_id=OTHER_DEVICE_ID)
    sink.consume(_artifact(tmp_path, ENV_RF_CONFIG_VALID), Reception(source="hub"))

    assert actuator.applied == [], "a genuine command for another node must not be executed here"


# --- Slice 4: structural rejects (all cheap checks, before the expensive verify) ------------

def test_unsupported_version_is_dropped(tmp_path):
    # Correctly signed for version=2, but this node only understands ENVELOPE_VERSION. Acting on
    # a format it does not understand is exactly the cross-version confusion the version guards.
    actuator = _CapturingActuator()
    _sink(actuator).consume(_artifact(tmp_path, ENV_BAD_VERSION_VALID), Reception(source="hub"))
    assert actuator.applied == [], "an unsupported envelope version must be dropped"


def test_undefined_or_unactuatable_type_is_dropped(tmp_path):
    # Correctly signed and correctly targeted, but type=0x7f has no actuator in this release. A
    # validly-signed-but-unactuatable artifact is dropped at the gate, never forwarded (and never
    # re-pulled): forwarding it would trap a would-be actuator in a retry loop.
    actuator = _CapturingActuator()
    _sink(actuator).consume(_artifact(tmp_path, ENV_UNKNOWN_TYPE_VALID), Reception(source="hub"))
    assert actuator.applied == [], "a type with no actuator must be dropped at the gate"


def test_truncated_envelope_is_dropped_not_crashed(tmp_path):
    # A file too short to even hold the fixed header+sig must be dropped cleanly, not indexed into.
    actuator = _CapturingActuator()
    _sink(actuator).consume(_artifact(tmp_path, b"\x01\x01tiny"), Reception(source="hub"))
    assert actuator.applied == [], "a truncated envelope must be dropped"


def test_reset_is_a_live_type_and_carries_an_empty_payload(tmp_path):
    actuator = _CapturingActuator()
    _sink(actuator).consume(_artifact(tmp_path, ENV_RESET_VALID), Reception(source="hub"))
    assert actuator.applied == [(RESET, b"")], "RESET must forward with its empty payload"


# --- Slice 5: fail closed at construction (never a silent no-verify) ------------------------

def test_missing_control_root_is_a_construction_error():
    # The whole point: an actuator wired without a control_root must refuse to start,
    # loudly, rather than silently forward unverified commands.
    with pytest.raises(ValueError):
        Control_Root_DataSink(control_root=None, device_id=TARGET_DEVICE_ID,
                              actuator=_CapturingActuator())
    with pytest.raises(ValueError):
        Control_Root_DataSink(control_root="", device_id=TARGET_DEVICE_ID,
                              actuator=_CapturingActuator())


def test_malformed_control_root_is_a_construction_error():
    # A syntactically-hex but off-curve / wrong-length key is a provisioning mistake; catch it at
    # construction, not per artifact. 0x04 || 64 zero bytes is a valid length but not on P-256.
    bad_offcurve = ("04" + "00" * 64)
    with pytest.raises(ValueError):
        _sink(_CapturingActuator(), control_root=bad_offcurve)
    with pytest.raises(ValueError):
        _sink(_CapturingActuator(), control_root="not-hex-at-all")


def test_missing_actuator_is_a_construction_error():
    with pytest.raises(ValueError):
        Control_Root_DataSink(control_root=CONTROL_ROOT_HEX, device_id=TARGET_DEVICE_ID,
                              actuator=None)


def test_wrong_length_device_id_is_a_construction_error():
    with pytest.raises(ValueError):
        _sink(_CapturingActuator(), device_id=b"\x00\x01\x02\x03")   # 4 B, not 32
    with pytest.raises(ValueError):
        _sink(_CapturingActuator(), device_id=None)


# --- Slice 6: an actuation failure propagates (retry), unlike a verification reject ---------

class _FailingActuator(Control_Actuator):
    def apply(self, control_type, payload):
        raise RuntimeError("actuator busy")


def test_actuation_failure_propagates_for_at_least_once_retry(tmp_path):
    # A *verification* failure is swallowed (drop, transfer completes). An *actuation* failure is
    # the opposite: it must propagate so the drive loop rewinds and re-pulls, giving the genuine
    # command at-least-once delivery. Guarding this so a later refactor can't quietly swallow it.
    with pytest.raises(RuntimeError):
        _sink(_FailingActuator()).consume(
            _artifact(tmp_path, ENV_RF_CONFIG_VALID), Reception(source="hub"))


# --- Slice 7: the reassembly temp is released (no on-device FD/temp leak) -------------------

def test_reassembly_temp_is_released_on_accept_and_reject(tmp_path):
    # A control artifact is executed, not stored: the decorator must free the reassembly temp
    # (a held-open FD on-device) whether the artifact is accepted or rejected.
    accepted = _artifact(tmp_path, ENV_RF_CONFIG_VALID, name="ok.bin")
    temp_ok = accepted.temp_file_path
    assert os.path.exists(temp_ok)
    _sink(_CapturingActuator()).consume(accepted, Reception(source="hub"))
    assert not os.path.exists(temp_ok), "a verified artifact left its reassembly temp behind"

    forged = bytearray(ENV_RF_CONFIG_VALID)
    forged[-1] ^= 0x01
    rejected = _artifact(tmp_path, bytes(forged), name="bad.bin")
    temp_bad = rejected.temp_file_path
    assert os.path.exists(temp_bad)
    _sink(_CapturingActuator()).consume(rejected, Reception(source="hub"))
    assert not os.path.exists(temp_bad), "a rejected artifact left its reassembly temp behind"

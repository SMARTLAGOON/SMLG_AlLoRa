"""Unit — the control-root verify layer (the Edge-side downlink gate).

A downlink control artifact (RF config change, reset, later a model/OTA) is authenticated
against a pinned per-deployment control-root public key *before* the Edge acts on it. The
signed artifact is ordinary file content carried by the reversed-pull transfer; the wire
(the frozen v3 packet) is untouched. This layer sits on the Edge's downlink DataSink,
operates on the fully reassembled artifact, and is the gate, not the actuator: on a verified
artifact it hands the wrapped actuator the trusted (control_type, payload); on anything
that fails it drops the artifact and never forwards.

Envelope:  version(1) || type(1) || target_device_id(32) || counter(4) || payload || sig(64)
           sig = ECDSA-P256 over SHA-256( everything before it ), raw r||s.

The envelopes below are minted at test time from a control-root keypair rather than pasted in
as frozen literals: the library can sign now, so a format change can re-sign its own fixtures
instead of needing a generator script that is no longer around. This does not leave the tests
marking their own homework, because the signature primitives are pinned independently against
RFC 6979's published vectors in test_ec_p256.py. What is under test here is the envelope
structure and the gate's policy, which is our own format and has no external vector to hold it
to anyway.
"""
import hashlib
import math
import os

import pytest

from AlLoRa.File import AlLoRa_File
from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.Control.Control_Actuator import Control_Actuator
from AlLoRa.Control.control_types import RF_CONFIG, RESET, MODEL, OTA
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink, ENVELOPE_VERSION
from AlLoRa.Security.ec_p256 import ecdsa_sign, public_key_uncompressed

# --- the deployment's control root, and the artifacts it mints -----------------------------

# RFC 6979 A.2.5's published test scalar, used here because it is unmistakably not a real key.
CONTROL_ROOT_PRIV = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721
CONTROL_ROOT_PUB = public_key_uncompressed(CONTROL_ROOT_PRIV)
CONTROL_ROOT_HEX = CONTROL_ROOT_PUB.hex()

# An unrelated authority: valid keys, no standing with a node pinned to the root above.
OTHER_ROOT_PRIV = 0x1F2E3D4C5B6A79889796A5B4C3D2E1F00F1E2D3C4B5A69788897A6B5C4D3E2F1
TARGET_DEVICE_ID = bytes.fromhex(
    "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
OTHER_DEVICE_ID = bytes.fromhex(
    "808182838485868788898a8b8c8d8e8f909192939495969798999a9b9c9d9e9f"
)
RF_PAYLOAD = b'{"sf":9,"bw":125,"tx_power":14}'


def _envelope(control_type, payload, target=TARGET_DEVICE_ID, counter=1,
              version=ENVELOPE_VERSION, priv=CONTROL_ROOT_PRIV):
    """Mint one control artifact, signed the way the gate expects to find it."""
    region = (bytes([version, control_type]) + bytes(target)
              + counter.to_bytes(4, "big") + bytes(payload))
    return region + ecdsa_sign(priv, hashlib.sha256(region).digest())


ENV_RF_CONFIG_VALID = _envelope(RF_CONFIG, RF_PAYLOAD)
ENV_RESET_VALID = _envelope(RESET, b"")
# Validly signed and correctly targeted, but a type with no actuator in this release.
ENV_UNKNOWN_TYPE_VALID = _envelope(0x7F, RF_PAYLOAD)
# Validly signed, in a format version this node does not understand.
ENV_BAD_VERSION_VALID = _envelope(RF_CONFIG, RF_PAYLOAD, version=ENVELOPE_VERSION + 1)


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


def _sink(actuator, device_id=TARGET_DEVICE_ID, control_root=CONTROL_ROOT_HEX,
          counter_file=None):
    return Control_Root_DataSink(control_root=control_root, device_id=device_id,
                                 actuator=actuator, counter_file=counter_file)


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
    # Correctly signed, but in a format version this node does not understand. Acting on a
    # format it cannot parse is exactly the cross-version confusion the version guards.
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


# --- Slice 8: freshness, so authority is end-to-end and not just unforgeable ----------------
#
# Without a counter a signed artifact is valid forever, and freshness is delegated to whoever
# carries it. That defeats the property the control root exists for: a Hub that cannot forge a
# command can still replay one it carried before, re-imposing a recorded config at will with a
# perfectly valid signature. The counter lives inside the signed region and a node refuses any
# artifact whose counter it has already passed.


def test_a_replayed_artifact_is_refused_the_second_time(tmp_path):
    # The same bytes, the same valid signature, delivered twice. The first is genuine; the
    # second is a replay and must not act.
    actuator = _CapturingActuator()
    sink = _sink(actuator)
    artifact = _envelope(RF_CONFIG, RF_PAYLOAD)

    sink.consume(_artifact(tmp_path, artifact, name="first.bin"), Reception(source="hub"))
    sink.consume(_artifact(tmp_path, artifact, name="again.bin"), Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], \
        "a replayed control artifact must be acted on once, not once per delivery"


def test_a_later_artifact_is_still_accepted_after_an_earlier_one(tmp_path):
    # The counter refuses what came before, never what comes next: an operator must be able to
    # keep reconfiguring the node.
    actuator = _CapturingActuator()
    sink = _sink(actuator)

    sink.consume(_artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=4), name="a.bin"),
                 Reception(source="hub"))
    sink.consume(_artifact(tmp_path, _envelope(RESET, b"", counter=9), name="b.bin"),
                 Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD), (RESET, b"")]


def test_an_artifact_reminted_at_the_same_counter_is_refused(tmp_path):
    # Strictly greater, not greater-or-equal. A fresh signature over the same counter is the
    # authority repeating itself, and the node has already acted on that instruction.
    actuator = _CapturingActuator()
    sink = _sink(actuator)

    sink.consume(_artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=4), name="a.bin"),
                 Reception(source="hub"))
    sink.consume(_artifact(tmp_path, _envelope(RESET, b"", counter=4), name="b.bin"),
                 Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], "the counter must be strictly greater"


def test_the_high_water_mark_survives_a_reboot(tmp_path):
    # A mark held only in RAM resets on restart and re-opens the entire replay window, which is
    # most of what the counter buys. Power-cycling the node must not make a captured artifact
    # replayable again.
    mark = str(tmp_path / "control.mark")
    artifact = _envelope(RF_CONFIG, RF_PAYLOAD, counter=5)

    before = _CapturingActuator()
    _sink(before, counter_file=mark).consume(
        _artifact(tmp_path, artifact, name="before.bin"), Reception(source="hub"))
    assert before.applied == [(RF_CONFIG, RF_PAYLOAD)]

    after_reboot = _CapturingActuator()          # a fresh gate reading the mark off the filesystem
    _sink(after_reboot, counter_file=mark).consume(
        _artifact(tmp_path, artifact, name="after.bin"), Reception(source="hub"))

    assert after_reboot.applied == [], "the replay window re-opened across a reboot"


def test_rotating_the_control_root_starts_the_count_over(tmp_path):
    # How a reset happens with nothing added to the wire: a mark is keyed by the root that
    # accepted it, so a new root does not match and counts from zero. Artifacts signed by the
    # old root no longer verify anyway, so nothing old becomes replayable.
    mark = str(tmp_path / "control.mark")
    seasoned = _CapturingActuator()
    _sink(seasoned, counter_file=mark).consume(
        _artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=900), name="old.bin"),
        Reception(source="hub"))
    assert seasoned.applied == [(RF_CONFIG, RF_PAYLOAD)]

    # Re-provisioned onto a different authority, whose own numbering starts low.
    rotated = _CapturingActuator()
    new_root = public_key_uncompressed(OTHER_ROOT_PRIV)
    _sink(rotated, control_root=new_root.hex(), counter_file=mark).consume(
        _artifact(tmp_path, _envelope(RESET, b"", counter=1, priv=OTHER_ROOT_PRIV),
                  name="new.bin"),
        Reception(source="hub"))

    assert rotated.applied == [(RESET, b"")], \
        "a rotated root must not inherit the previous root's high-water mark"


def test_a_refused_artifact_does_not_move_the_mark(tmp_path):
    # The mark moves only on an artifact that verified. Otherwise anyone able to put bytes in
    # front of the node could send a forged artifact with a huge counter and lock it out of
    # every genuine command below that number.
    actuator = _CapturingActuator()
    sink = _sink(actuator)
    forged = bytearray(_envelope(RF_CONFIG, RF_PAYLOAD, counter=10_000))
    forged[-1] ^= 0x01

    sink.consume(_artifact(tmp_path, bytes(forged), name="forged.bin"), Reception(source="hub"))
    sink.consume(_artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=2), name="ok.bin"),
                 Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], \
        "a forged counter must not raise the bar for genuine commands"


def test_a_failed_actuation_leaves_the_artifact_deliverable_again(tmp_path):
    # The gate already lets an actuation failure propagate so the drive loop re-pulls the same
    # artifact. That retry only works if the mark has not moved yet: marking on verify rather
    # than on actuation would refuse the node's own retry as a replay and lose the command.
    artifact = _envelope(RF_CONFIG, RF_PAYLOAD, counter=3)
    failing = _sink(_FailingActuator())
    with pytest.raises(RuntimeError):
        failing.consume(_artifact(tmp_path, artifact, name="try1.bin"), Reception(source="hub"))

    assert failing.counter == 0, "a command that never actuated must not count as delivered"

    recovered = _CapturingActuator()
    failing.actuator = recovered                 # the transient condition clears
    failing.consume(_artifact(tmp_path, artifact, name="try2.bin"), Reception(source="hub"))

    assert recovered.applied == [(RF_CONFIG, RF_PAYLOAD)], "the re-pull must still be accepted"


# --- the gate on a MicroPython hashlib -----------------------------------------------------

# Bound before anything patches the name: the stand-in below has to hash with the real
# implementation, and hashlib is one shared module object, so patching it would otherwise send
# the stand-in straight back into itself.
_REAL_SHA256 = hashlib.sha256


class _Micropython_sha256:
    """CPython's sha256 minus hexdigest, which is what a board actually provides.

    MicroPython's hashlib offers digest() and nothing else. Anything the gate reaches for
    beyond that passes every test here and throws on every device, so the only way to hold
    this layer to the target runtime is to take the extra methods away.
    """

    def __init__(self, data=b""):
        self._h = _REAL_SHA256(data)

    def update(self, data):
        self._h.update(data)

    def digest(self):
        return self._h.digest()


def test_the_mark_persists_on_a_runtime_whose_sha256_has_no_hexdigest(monkeypatch, tmp_path):
    # Found on hardware: the gate fingerprinted the root with hexdigest(), which MicroPython
    # does not have. The throw escaped _accept_counter's OSError-only catch and aborted the
    # reception *after* a successful verify, so the peer never heard the final OK, never
    # mirrored the change, and the two ends finished on different configurations. The command
    # itself had already been actuated, which is the split this whole layer exists to avoid.
    #
    # Minting happens first, on the real hashlib, because that is where it happens for real:
    # the authority signs on a laptop or a backend, and only this gate runs on the board.
    # Signing also needs an HMAC, which wants more of the hash object than a board exposes.
    mark = str(tmp_path / "control.mark")
    first = _artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=5))
    replay = _artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=5), name="replay.bin")

    monkeypatch.setattr(
        "AlLoRa.DataSinks.Control_Root_DataSink.hashlib.sha256", _Micropython_sha256)

    actuator = _CapturingActuator()
    _sink(actuator, counter_file=mark).consume(first, Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)], "the artifact must still actuate"
    assert os.path.getsize(mark) > 0, \
        "an empty mark reads back as 'no counter' and re-opens the replay window every boot"

    # The reboot half: the mark is only worth writing if the next boot can read it back.
    after = _CapturingActuator()
    _sink(after, counter_file=mark).consume(replay, Reception(source="hub"))
    assert after.applied == [], "a captured artifact must not replay across a reboot"


def test_a_mark_that_cannot_be_written_still_completes_the_transfer(tmp_path):
    # Persisting is best-effort. Whatever goes wrong writing it, the accepted command must not
    # come back to the caller as a failed delivery: that is what turns a working reconfiguration
    # into two ends on different configurations.
    actuator = _CapturingActuator()
    sink = _sink(actuator, counter_file=str(tmp_path / "nope" / "deep" / "control.mark"))

    sink.consume(_artifact(tmp_path, _envelope(RF_CONFIG, RF_PAYLOAD, counter=2)),
                 Reception(source="hub"))

    assert actuator.applied == [(RF_CONFIG, RF_PAYLOAD)]
    assert sink.counter == 2, "the RAM mark still has to hold for this boot"

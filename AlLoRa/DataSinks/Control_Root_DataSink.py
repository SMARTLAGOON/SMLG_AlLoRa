import hashlib

from AlLoRa.Control.control_types import RF_CONFIG, RESET
from AlLoRa.Control.control_envelope import (
    ENVELOPE_VERSION, HEADER_LEN as _HEADER_LEN, MIN_LEN as _MIN_LEN,
    SIG_LEN as _SIG_LEN, TARGET_LEN as _TARGET_LEN)
from AlLoRa.DataSinks.DataSink import DataSink
from AlLoRa.Security.ec_p256 import ecdsa_verify, decode_public_key
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.json_utils import json

# The envelope layout (and the version that names it) is shared with the minting side, so the
# two ends cannot drift; ENVELOPE_VERSION is re-exported here because this module was where
# callers first found it.

# Only types with an actuator in this release are forwarded. A validly-signed but not-yet-
# actuatable type (MODEL/OTA reserved) or an undefined byte is dropped at the gate, never
# forwarded and never re-pulled. Extend this tuple as actuators land.
_LIVE_TYPES = (RF_CONFIG, RESET)


class Control_Root_DataSink(DataSink):
    """The verify gate of the control path: a DataSink that only forwards what is authentic.

    It takes a downlink file off the air like any other sink, checks the envelope and the
    signature against the control root, and hands the verified artifact to a `Control_Actuator`,
    which owns the effect and the timing of it. All the crypto lives here and no device
    knowledge does, so the same gate is reused on any node with any actuator behind it.
    """

    def __init__(self, control_root, device_id, actuator, counter_file=None):
        # Fail closed at construction: a mis-provisioned gate must refuse to start, never
        # silently forward unverified commands.
        if not control_root:
            raise ValueError(
                "verifying a control artifact requires a control_root public key: refusing to "
                "run unverified (a registered node must never act on an unsigned command)")
        if actuator is None:
            raise ValueError("Control_Root_DataSink wraps an actuator; none was given")
        if device_id is None or len(device_id) != _TARGET_LEN:
            raise ValueError(
                "device_id must be this node's 32-byte identity fingerprint (its device_id)")
        self.control_root = self._load_control_root(control_root)
        self.device_id = bytes(device_id)
        self.actuator = actuator
        self.counter_file = counter_file
        self.counter = self._load_counter()

    @staticmethod
    def _root_fingerprint(root_key):
        return hashlib.sha256(root_key).hexdigest()

    def _load_counter(self):
        """Read back the highest counter this node has accepted, or 0 if it has none.

        The mark is persisted because a RAM-only one resets on reboot and re-opens the whole
        replay window, which is most of what the counter buys. It is keyed by the root that
        accepted it, which is also how a reset happens without a command for it: a rotated
        control root does not match the stored fingerprint, so the count starts over, and a
        re-provisioned node has a new device_id that old artifacts no longer address. Given no
        file (a board with no filesystem) the mark is RAM-only and the window does re-open at
        reboot: a provisioning fact to know about, not a choice made here.
        """
        if not self.counter_file:
            return 0
        try:
            with open(self.counter_file, "r") as f:
                mark = json.loads(f.read())
            if mark.get("root") != self._root_fingerprint(self.control_root):
                return 0
            counter = mark.get("counter", 0)
        except (OSError, ValueError, AttributeError):
            # No mark yet, or one we cannot read. Starting from 0 costs freshness, never
            # authenticity: every artifact still has to verify against the control root.
            return 0
        return counter if isinstance(counter, int) and counter > 0 else 0

    def _accept_counter(self, counter):
        self.counter = counter
        if not self.counter_file:
            return
        try:
            with open(self.counter_file, "w") as f:
                f.write(json.dumps({"root": self._root_fingerprint(self.control_root),
                                    "counter": counter}))
        except OSError as e:
            # A read-only or full filesystem must not turn an accepted command into a failed
            # one: the RAM mark still holds for this boot.
            print("Control_Root_DataSink: could not persist the control counter ({})".format(e))

    @staticmethod
    def _load_control_root(control_root):
        # SEC1 hex (a config string) or a raw 65-byte key. Decode + validate once here so a bad
        # or off-curve key is a loud provisioning error now, not a silent per-artifact failure.
        key = bytes.fromhex(control_root) if isinstance(control_root, str) else bytes(control_root)
        decode_public_key(key)   # raises ValueError on a wrong-length / off-curve key
        return key

    def consume(self, file, reception=None):
        content = file.get_content()
        # The artifact is now in RAM (a control artifact is executed, not stored): free the
        # reassembly temp on every path. A later re-pull, if apply() fails below, starts a fresh
        # buffer regardless, and File.discard is a safe no-op if the drive loop discards too.
        try:
            file.discard()
        except Exception:
            pass
        verified = self._verify(content)
        if verified is None:
            return   # rejected: dropped. NEVER raise here -> the transfer still completes (the
                     # final-OK is sent) and the identical bytes are not re-pulled forever.
        control_type, payload, counter = verified
        # Authentic and for us. An actuation failure inside apply() is transient and is allowed
        # to propagate: the drive loop rewinds and re-pulls, giving at-least-once delivery.
        self.actuator.apply(control_type, payload)
        # Only once the command has actually been actuated. Marking the counter first would
        # make the re-pull that follows a failed apply() look like a replay, and the retry the
        # line above exists for would be refused.
        self._accept_counter(counter)

    def _verify(self, content):
        # Cheap structural checks first, the one expensive ECDSA last (on-device CPU is sacred):
        # length -> version -> type -> target -> counter -> signature.
        if len(content) < _MIN_LEN:
            return self._reject("truncated envelope: {} B < {}".format(len(content), _MIN_LEN))
        mv = memoryview(content)
        version = mv[0]
        if version != ENVELOPE_VERSION:
            return self._reject("unsupported envelope version {}".format(version))
        control_type = mv[1]
        if control_type not in _LIVE_TYPES:
            return self._reject("no actuator for control type {}".format(control_type))
        if bytes(mv[2:2 + _TARGET_LEN]) != self.device_id:
            return self._reject("artifact addressed to another node")
        # Freshness, and deliberately ahead of the signature: a replay is then refused without
        # paying the seconds an ECDSA verify costs on-device. A forged high counter still costs
        # one verify, which is no worse than having no counter at all.
        counter = int.from_bytes(bytes(mv[2 + _TARGET_LEN:_HEADER_LEN]), "big")
        if counter <= self.counter:
            return self._reject(
                "stale counter {} (already accepted {})".format(counter, self.counter))
        region = mv[:-_SIG_LEN]
        sig = mv[-_SIG_LEN:]
        if not ecdsa_verify(self.control_root, hashlib.sha256(region).digest(), sig):
            return self._reject("signature does not verify against the control root")
        return control_type, bytes(mv[_HEADER_LEN:-_SIG_LEN]), counter

    def _reject(self, reason):
        # A rejected control artifact is a rare, security-relevant event: always log it. Returning
        # None tells consume() to drop the artifact (never raise -> no re-pull loop).
        print("Control_Root_DataSink: rejected control artifact ({})".format(reason))
        return None

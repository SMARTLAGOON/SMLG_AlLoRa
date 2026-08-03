import hashlib

from AlLoRa.Control.control_types import RF_CONFIG, RESET
from AlLoRa.DataSinks.DataSink import DataSink
from AlLoRa.Security.ec_p256 import ecdsa_verify, decode_public_key
from AlLoRa.utils.debug_utils import print

# Envelope format version (in the signed region). Bumped only when the byte-layout changes.
ENVELOPE_VERSION = 1

# Only types with an actuator in this release are forwarded. A validly-signed but not-yet-
# actuatable type (MODEL/OTA reserved) or an undefined byte is dropped at the gate, never
# forwarded and never re-pulled. Extend this tuple as actuators land.
_LIVE_TYPES = (RF_CONFIG, RESET)

_SIG_LEN = 64
_TARGET_LEN = 32
_HEADER_LEN = 1 + 1 + _TARGET_LEN          # version, type, target_device_id
_MIN_LEN = _HEADER_LEN + _SIG_LEN          # 98: the fixed overhead with an empty payload


class Control_Root_DataSink(DataSink):
    """The verify gate of the control path: a DataSink that only forwards what is authentic.

    It takes a downlink file off the air like any other sink, checks the envelope and the
    signature against the control root, and hands the verified artifact to a `Control_Actuator`,
    which owns the effect and the timing of it. All the crypto lives here and no device
    knowledge does, so the same gate is reused on any node with any actuator behind it.
    """

    def __init__(self, control_root, device_id, actuator):
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
        control_type, payload = verified
        # Authentic and for us. An actuation failure inside apply() is transient and is allowed
        # to propagate: the drive loop rewinds and re-pulls, giving at-least-once delivery.
        self.actuator.apply(control_type, payload)

    def _verify(self, content):
        # Cheap structural checks first, the one expensive ECDSA last (on-device CPU is sacred):
        # length -> version -> type -> target -> signature.
        if len(content) < _MIN_LEN:
            return self._reject("truncated envelope: {} B < {}".format(len(content), _MIN_LEN))
        mv = memoryview(content)
        version = mv[0]
        if version != ENVELOPE_VERSION:
            return self._reject("unsupported envelope version {}".format(version))
        control_type = mv[1]
        if control_type not in _LIVE_TYPES:
            return self._reject("no actuator for control type {}".format(control_type))
        if bytes(mv[2:_HEADER_LEN]) != self.device_id:
            return self._reject("artifact addressed to another node")
        region = mv[:-_SIG_LEN]
        sig = mv[-_SIG_LEN:]
        if not ecdsa_verify(self.control_root, hashlib.sha256(region).digest(), sig):
            return self._reject("signature does not verify against the control root")
        return control_type, bytes(mv[_HEADER_LEN:-_SIG_LEN])

    def _reject(self, reason):
        # A rejected control artifact is a rare, security-relevant event: always log it. Returning
        # None tells consume() to drop the artifact (never raise -> no re-pull loop).
        print("Control_Root_DataSink: rejected control artifact ({})".format(reason))
        return None

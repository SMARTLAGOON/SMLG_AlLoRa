"""The control root itself: the authority that mints what a node's verify gate accepts.

`Control_Root_DataSink` verifies against a pinned public key; this is the private half that
produces what it verifies. Both live in the library on purpose. A deployment with a backend
lets the backend mint and the Hub only carry the result; a deployment without one gives its
Hub the root private key and lets it mint locally, which is what makes a self-contained
installation able to reconfigure itself at all.

Two costs of that, worth naming rather than burying. The signing code is frozen into every
node, including Edges that will never sign (small, since the curve primitives were already
there for the handshake). And a minting Hub holds a private key on the board: that is the
operator's trade, and it is why holding one is a deliberate act of provisioning rather than
something that happens by default.

A better delegation model exists and is deliberately not built here: a scoped, short-lived
authority signed by the root would let a Hub retune a node without ever holding the key that
can also reset or re-flash it. That is a later increment, not a different design.
"""
import hashlib

from AlLoRa.Control.control_envelope import (
    ENVELOPE_VERSION, MAX_COUNTER, TARGET_LEN, signed_region)
from AlLoRa.Security.ec_p256 import (
    N, ecdsa_sign, generate_private_key, public_key_uncompressed)


class Control_Root:
    """Holds a control-root private key and mints signed control artifacts with it."""

    def __init__(self, private_key):
        # A hex string (what a key file holds) or the scalar itself. Validated here so a
        # mis-provisioned root is a loud error at startup rather than a stream of artifacts
        # no node will accept.
        if isinstance(private_key, str):
            try:
                private_key = int(private_key.strip(), 16)
            except ValueError:
                raise ValueError("control-root private key must be hex")
        if not isinstance(private_key, int) or not (1 <= private_key < N):
            raise ValueError("control-root private key must be a P-256 scalar in [1, N)")
        self._priv = private_key
        self._pub = public_key_uncompressed(private_key)

    @classmethod
    def generate(cls, randfunc):
        """Create a brand-new control root. ``randfunc(nbytes)`` supplies the entropy (e.g.
        ``os.urandom``), injected so the caller owns that choice, as key generation elsewhere
        in the library does. This is the one operation here that needs an RNG: signing itself
        derives its nonce and needs none."""
        return cls(generate_private_key(randfunc))

    def public_key(self):
        """The SEC1 uncompressed public key to provision onto the nodes this root commands."""
        return self._pub

    def public_key_hex(self):
        """The same key in the hex form a config file or a key file carries."""
        return self._pub.hex()

    def fingerprint(self):
        """SHA-256 of the public key: how a node keys the counter mark to the root that set
        it, and a short way for an operator to tell two roots apart."""
        return hashlib.sha256(self._pub).digest()

    def mint(self, control_type, target_device_id, counter, payload=b""):
        """Return a signed control artifact for one node.

        `counter` must be higher than any the target has already accepted, or the target will
        refuse it as a replay. The numbering belongs to whoever operates the root, since only
        that side knows what it has issued; this class does not keep it, so that a Hub and a
        backend minting for the same fleet cannot each keep their own idea of it.

        It has no default on purpose. A default would mint a second artifact carrying the
        number the first already used, which the target refuses as a replay: the command
        simply never lands, and nothing on either side says why.
        """
        if not isinstance(control_type, int) or not (0 <= control_type <= 0xFF):
            raise ValueError("control_type must be a single byte")
        if target_device_id is None or len(target_device_id) != TARGET_LEN:
            raise ValueError(
                "target_device_id must be the target node's 32-byte identity fingerprint")
        if not isinstance(counter, int) or not (1 <= counter <= MAX_COUNTER):
            raise ValueError("counter must be in [1, {}]".format(MAX_COUNTER))
        region = signed_region(control_type, target_device_id, counter, payload,
                               version=ENVELOPE_VERSION)
        return region + ecdsa_sign(self._priv, hashlib.sha256(region).digest())

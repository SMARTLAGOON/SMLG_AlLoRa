"""A v3 node's crypto-bound identity — the fingerprint of its long-term public key.

The successor to MAC-as-identity. v2 addressed and "identified" a node by its wifi MAC, a
value anyone can claim. v3 derives identity from a key the node holds: device_id =
SHA256(pubkey). The operator registers the fingerprint on the Collector exactly like copying
a MAC today, but now it names a keypair, not a spoofable address.

The long-term identity key is distinct from the per-session ephemeral used in the ECDH: the
ephemeral changes every session (that is what makes a reboot re-key into fresh material),
so it can't be a stable identity. The identity key lives across reboots; its fingerprint is
what stays registered.

Wire use of the fingerprint:
  * device_id[:4] addresses first contact (one 4-byte token, no MAC on the wire);
  * device_id[0]  seeds the 1-byte session id once a session exists.
"""
import hashlib

from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed


def device_id_from_pubkey(pubkey):
    """The 32-byte identity fingerprint of a SEC1 public key: SHA256(pubkey)."""
    return hashlib.sha256(pubkey).digest()


def load_or_create_identity(path, randfunc):
    """Return this node's long-term identity as ``(priv, pubkey, device_id)``, persisting the
    private scalar to ``path`` so the device_id survives reboots (a stable device_id is what
    keeps the operator's registration valid). The scalar is stored as 64 hex chars; on first
    boot it is generated and written, thereafter it is read back.
    """
    try:
        with open(path, "r") as f:
            priv = int(f.read().strip(), 16)
    except OSError:
        priv = generate_private_key(randfunc)
        with open(path, "w") as f:
            f.write("{:064x}".format(priv))
    pub = public_key_uncompressed(priv)
    return priv, pub, device_id_from_pubkey(pub)

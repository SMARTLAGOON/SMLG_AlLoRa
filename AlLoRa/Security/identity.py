"""A v3 node's identity: the SHA-256 hash of its long-term public key (`device_id`).

It comes from the long-term key, not the per-session one. On the air, `device_id[:4]` addresses
first contact, and `device_id[0]` seeds the session id.
"""
import hashlib

from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
from AlLoRa.utils.file_utils import commit_file


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
        # Committed through a rename, because a key file written in place fails silently rather
        # than loudly. Half of a 64-character scalar still parses as an integer, so a node
        # interrupted here would come back working, with a stable device_id, and with a key of
        # half the intended size. Either the whole key is on disk or none of it is, and a node
        # with none simply generates another.
        commit_file(path, "{:064x}".format(priv))
    pub = public_key_uncompressed(priv)
    return priv, pub, device_id_from_pubkey(pub)

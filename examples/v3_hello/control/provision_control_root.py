#!/usr/bin/env python3
"""Creates a fleet's control root and writes its two halves into hub/ and edge/.

The Hub keeps the private half and signs commands with it. Each node keeps the public half and
from then on refuses unsigned commands. Running it again never replaces an existing root, because
every node is pinned to it. Keep the private half out of git. The steps are in this folder's README.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))

from AlLoRa.Control.Control_Root import Control_Root                # noqa: E402
from AlLoRa.Security.ec_p256 import generate_private_key            # noqa: E402

KEY_NAME = "control_root.key"


def provision(base):
    hub_dir = os.path.join(base, "hub")
    edge_dir = os.path.join(base, "edge")
    for d in (hub_dir, edge_dir):
        if not os.path.isdir(d):
            os.makedirs(d)
    hub_key = os.path.join(hub_dir, KEY_NAME)
    edge_key = os.path.join(edge_dir, KEY_NAME)

    if os.path.exists(hub_key):
        with open(hub_key, "r") as f:
            root = Control_Root(f.read().strip())
        print("Found an existing control root at {}, keeping it.".format(hub_key))
    else:
        # Written the way identity.key already stores a scalar, and written here rather than
        # exported from Control_Root: that object exists to sign with the key, and giving it a
        # method that hands the key back would make every node capable of exporting the fleet's
        # authority. Provisioning is a tool's job, not the library's.
        priv = generate_private_key(os.urandom)
        root = Control_Root(priv)
        with open(hub_key, "w") as f:
            f.write("{:064x}".format(priv))
        print("New control root written to {} (the signing half: keep it safe).".format(hub_key))

    with open(edge_key, "w") as f:
        f.write(root.public_key_hex())
    print("Verifying half written to {} (copy this one to every commanded node).".format(edge_key))
    print("Root fingerprint: {}".format(root.fingerprint().hex()))
    print("\nAdd to both AlLoRa.json files:  \"control_root_file\": \"{}\"".format(KEY_NAME))


if __name__ == "__main__":
    provision(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))

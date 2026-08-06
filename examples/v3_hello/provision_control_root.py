#!/usr/bin/env python3
"""Create a fleet's control root and lay out the two halves ready to copy onto the boards.

A control root is the authority that signs configuration commands: retune this node, reset
that one. The Hub (or a backend) holds the private half and signs with it; every commanded
node holds the public half and checks signatures against it. Provisioning one changes what a
node accepts, since a node that holds a control root refuses unsigned commands from then on.

    python3 provision_control_root.py                # writes into hub/ and edge/ next to this
    python3 provision_control_root.py /path/to/fleet # writes into <path>/hub and <path>/edge

Both files are called `control_root.key` on purpose: the same name and the same config line
work on either kind of node, exactly as `identity.key` already does, and what the file
contains is what decides the node's role. 64 hex characters is the signing half, 130 is the
verifying half.

Run it once per fleet, then add to BOTH `LoRa.json` files:

    "control_root_file": "control_root.key"

and copy each half to its board:

    ampy -p /dev/cu.usbmodemHUB  put hub/control_root.key  control_root.key
    ampy -p /dev/cu.usbmodemEDGE put edge/control_root.key control_root.key

Re-running it never replaces an existing root. That is deliberate: every node is pinned to the
root it was given, so a new one would lock the whole fleet out of its own control plane until
every board is re-provisioned by hand. On a second run it re-derives the public half instead,
which is what you want when adding a node to a fleet that already exists.

The private half is the fleet's authority. Keep it off shared drives and out of git; a node
that only obeys commands must never be given a copy.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

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
    print("\nAdd to both LoRa.json files:  \"control_root_file\": \"{}\"".format(KEY_NAME))


if __name__ == "__main__":
    provision(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))

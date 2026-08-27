"""The fleet directory: one deployment's authority, its mint counter, and its registry.

The rule that generates the right answer every time is that **the control root lives with the
operator, not with the radio**. Ask who decides to retune a node; that is where the private
half goes, and everything between that decision and the node is a courier, the Hub included.
This object is that operator-side home: a plain directory a person can see, back up and hand
over, rather than a hidden dotfile whose location becomes a mystery the day somebody has to
move it.

Three properties it exists to hold.

**A root is never replaced.** Every node is pinned to the root it was given, so minting a
fresh one over an existing fleet locks every board out of its own control plane until each is
re-provisioned by hand. A second run re-derives the public half instead, which is what adding
a node to an existing fleet needs.

**The counter belongs to the root**, in the same shape and keyed by the same fingerprint the
Hub uses, because `control_counter_file` on a signer holds the highest number *minted* and two
signers on one root both start at zero and both mint number 1. The second one's artifact is
refused as a replay, which looks exactly like the command not working. So a handover moves the
root and the counter together, or it moves neither.

**The registry is what a node was issued**, not what a board reports. It is the operator's
record, a superset of what a Hub needs, and `render_nodes_json` narrows it to the roster the
Hub actually reads. Keeping them separate is deliberate: the Hub writes settled radio settings
back into its own copy, and that write must not reach the operator's record of who is in the
fleet.
"""
import json
import os

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Security.ec_p256 import public_key_uncompressed
from AlLoRa.Security.identity import device_id_from_pubkey

# The filename a board's config line names, on both node kinds. Same name for both halves on
# purpose, exactly as `identity.key` already is: what the file *contains* decides whether the
# node signs commands or checks them.
CONTROL_ROOT_NAME = "control_root.key"

# The two lengths that tell the halves apart. A P-256 scalar is 32 bytes and a SEC1
# uncompressed public key is 65, both written as hex.
PRIVATE_HEX_LEN = 64
PUBLIC_HEX_LEN = 130

_COUNTER_NAME = "control.counter"
_REGISTRY_NAME = "fleet.json"
_BACKUPS_DIR = "backups"
_STAGING_DIR = "staging"

# The keys a Nodes.json entry may carry. The registry holds more than this (what posture a node
# was provisioned into, where its identity backup went), and none of that belongs in a file the
# Hub parses: an entry is read by `Digital_Endpoint`, which ignores what it does not know, so a
# stray key would travel to every deployment unnoticed rather than being rejected.
_ROSTER_KEYS = ("name", "device_id", "mac_address", "session_id", "active", "sleep_mesh",
                "asking_frequency", "listening_time", "lock_on_file_receive",
                "max_listen_time_when_locked", "stall_timeout", "connector")

_ROSTER_DEFAULTS = {
    "active": True,
    "sleep_mesh": False,
    "asking_frequency": 60,
    "listening_time": 30,
    "lock_on_file_receive": False,
    "max_listen_time_when_locked": 300,
}


def classify_root_half(material):
    """Return "private" or "public" for a control-root file's contents, by length.

    The same discrimination `Node._control_root_from` performs on the board, done here so the
    wizard can refuse to stage the wrong half rather than let a node halt on it. The failure
    this prevents is the worst one available: the fleet's signing key sitting on a field node,
    a key compromise hiding behind a working link.
    """
    material = material.strip()
    if len(material) == PRIVATE_HEX_LEN:
        return "private"
    if len(material) == PUBLIC_HEX_LEN:
        return "public"
    raise ValueError(
        "a control root is either a SEC1 public key ({} hex characters) or a P-256 private "
        "scalar ({}); this holds {}".format(PUBLIC_HEX_LEN, PRIVATE_HEX_LEN, len(material)))


class Fleet:
    """One deployment's operator-side directory."""

    def __init__(self, path):
        self.path = os.path.abspath(path)

    # --- layout -------------------------------------------------------------------------

    @property
    def root_key_path(self):
        return os.path.join(self.path, CONTROL_ROOT_NAME)

    @property
    def counter_path(self):
        return os.path.join(self.path, _COUNTER_NAME)

    @property
    def registry_path(self):
        return os.path.join(self.path, _REGISTRY_NAME)

    @property
    def backups_path(self):
        return os.path.join(self.path, _BACKUPS_DIR)

    @property
    def staging_path(self):
        return os.path.join(self.path, _STAGING_DIR)

    def ensure(self):
        for directory in (self.path, self.backups_path, self.staging_path):
            if not os.path.isdir(directory):
                os.makedirs(directory)
        return self.path

    # --- the root -----------------------------------------------------------------------

    def has_root(self):
        return os.path.exists(self.root_key_path)

    def load_or_create_root(self, randfunc=os.urandom):
        """Return ``(Control_Root, created)``, minting one only when the fleet has none."""
        self.ensure()
        if self.has_root():
            with open(self.root_key_path, "r") as f:
                material = f.read().strip()
            half = classify_root_half(material)
            if half != "private":
                raise ValueError(
                    "{} holds the public half of a control root. This directory is the "
                    "fleet's authority and needs the signing half (the {}-character scalar); "
                    "the public half is what goes on the nodes.".format(
                        self.root_key_path, PRIVATE_HEX_LEN))
            return Control_Root(material), False

        root = Control_Root.generate(randfunc)
        # Written here rather than exported from Control_Root: that object exists to sign with
        # the key, and giving it a method that hands the key back would make every node able to
        # export the fleet's authority. Provisioning is a tool's job, not the library's.
        self._write_private(self.root_key_path, root)
        return root, True

    def root(self):
        """The existing root, or None. Never mints: reading is not provisioning."""
        if not self.has_root():
            return None
        with open(self.root_key_path, "r") as f:
            return Control_Root(f.read().strip())

    def _write_private(self, path, root):
        # The scalar reaches the file the way Control_Root took it in, so a round trip through
        # this directory is byte-identical to what the board would be given.
        material = self._private_hex(root)
        with open(path, "w") as f:
            f.write(material)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass    # a filesystem without POSIX modes (a mounted share) still gets the file
        return path

    @staticmethod
    def _private_hex(root):
        return "{:064x}".format(root._priv)

    def stage_public_root(self):
        """Lay the verifying half where a commanded node's push can pick it up.

        Named `control_root.key` because that is the name the node's config line carries; the
        130 characters inside are what make it the verifying half.
        """
        root, _ = self.load_or_create_root()
        target_dir = os.path.join(self.staging_path, "public")
        if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        path = os.path.join(target_dir, CONTROL_ROOT_NAME)
        with open(path, "w") as f:
            f.write(root.public_key_hex())
        return path

    def stage_private_root(self):
        """Lay the signing half out for the on-site root mode, and only for it.

        A separate call from `stage_public_root` rather than a flag on it, so that no code path
        can reach the private half by passing the wrong argument. The caller that wants this
        has to say the words.
        """
        root, _ = self.load_or_create_root()
        target_dir = os.path.join(self.staging_path, "private")
        if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        return self._write_private(os.path.join(target_dir, CONTROL_ROOT_NAME), root)

    # --- the counter --------------------------------------------------------------------

    def counter(self):
        """The highest number this fleet has minted under its current root, or 0.

        Keyed by the root's fingerprint exactly as `Hub._load_control_counter` keys its own, so
        a counter left behind by a rotated root says nothing about this one. That makes
        rotating the root the single reset for the whole control plane rather than a second
        mechanism to get right.
        """
        root = self.root()
        if root is None:
            return 0
        try:
            with open(self.counter_path, "r") as f:
                mark = json.load(f)
            if mark.get("root") != root.fingerprint().hex():
                return 0
            value = mark.get("counter", 0)
        except Exception:
            return 0
        return value if isinstance(value, int) and value > 0 else 0

    def next_counter(self):
        """Reserve and persist the next number, then hand it out.

        Recorded before it is returned, for the same reason the Hub records first: losing the
        write after minting re-issues a number the target has already accepted, and that
        artifact is refused as a replay with nothing on either side saying why. Losing it the
        other way merely burns a number, and a gap costs nothing.
        """
        root, _ = self.load_or_create_root()
        value = self.counter() + 1
        self._commit(self.counter_path,
                     json.dumps({"root": root.fingerprint().hex(), "counter": value}))
        return value

    # --- the registry -------------------------------------------------------------------

    def entries(self):
        try:
            with open(self.registry_path, "r") as f:
                data = json.load(f)
        except Exception:
            return []
        return data if isinstance(data, list) else []

    def register(self, name, role, device_id=None, posture="secure", **fields):
        """Record a node the operator has provisioned, keyed by its `device_id`.

        Keyed by the fingerprint and not by the name, because the fingerprint is the thing the
        board proves it holds: a board that keeps its identity across a reflash is the same
        node however it is labelled afterwards, and registering it twice would leave the Hub
        polling one node under two entries. An open node has no fingerprint to be keyed by --
        that is the whole difference the posture makes -- so it falls back to its placement and
        its name.

        An absent `device_id` is left out of the entry rather than written as an empty string.
        `Digital_Endpoint` reads a registered fingerprint as hex and then indexes it to derive
        the session address, so an empty one is not "no identity": it is a zero-length
        identity, and the Hub raises on it at boot.
        """
        entry = dict(_ROSTER_DEFAULTS)
        entry.update({"name": name, "role": role, "posture": posture})
        if device_id:
            entry["device_id"] = device_id
        entry.update({k: v for k, v in fields.items() if v is not None})

        registry = self.entries()
        for index, existing in enumerate(registry):
            same = (existing.get("device_id") == device_id if device_id
                    else (not existing.get("device_id")
                          and existing.get("role") == role and existing.get("name") == name))
            if same:
                merged = dict(existing)
                merged.update(entry)
                registry[index] = merged
                entry = merged
                break
        else:
            registry.append(entry)

        self.ensure()
        self._commit(self.registry_path, json.dumps(registry, indent=2) + "\n")
        return entry

    def render_nodes_json(self):
        """The Hub's roster: the Edges in this fleet, in the shape `Nodes.json` names.

        Registration goes in a file rather than a constant pasted into `main.py` because this
        is what `Hub.add_digital_endpoints` already reads and what the Hub already writes
        settled radio settings back into, and because a backend can read and edit a file where
        it cannot edit a Python constant.
        """
        roster = []
        for entry in self.entries():
            if entry.get("role") != "edge":
                continue
            roster.append({k: entry[k] for k in _ROSTER_KEYS
                           if k in entry and entry[k] is not None and entry[k] != ""})
        return json.dumps(roster, indent=2) + "\n"

    # --- identity backups ---------------------------------------------------------------

    def backup_identity(self, label, material):
        """Keep a copy of a board's `identity.key` before anything erases it.

        The flash wipes the board filesystem and the `device_id` is derived from this scalar,
        so a reflash without a backup silently invalidates the operator's registration and, on
        a bench board, the provenance of every measurement published under that fingerprint.
        Never overwrites: a second backup that replaced the first would be no backup at all, so
        each one gets its own numbered file and the operator can see how many there are.
        """
        self.ensure()
        index = 1
        while True:
            path = os.path.join(self.backups_path, "{}-{:02d}.identity.key".format(label, index))
            if not os.path.exists(path):
                break
            index += 1
        with open(path, "w") as f:
            f.write(material.strip())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    @staticmethod
    def device_id_of_identity_file(path):
        """The `device_id` a board holding this scalar would compute, as hex.

        Run through the library's own crypto rather than a second implementation, which is the
        whole reason the wizard lives inside the library: this value has to match what the
        board prints, byte for byte, or a restored backup registers a node that will never
        answer.
        """
        with open(path, "r") as f:
            priv = int(f.read().strip(), 16)
        return device_id_from_pubkey(public_key_uncompressed(priv)).hex()

    # --- writes -------------------------------------------------------------------------

    @staticmethod
    def _commit(path, text):
        # Through a rename, like the library commits its own config and counter files. A
        # truncated counter reads back as no counter at all, which starts the sequence over,
        # and a fleet only takes numbers above the highest it has already accepted.
        temp = path + ".tmp"
        with open(temp, "w") as f:
            f.write(text)
        os.replace(temp, path)
        return path

"""The `AlLoRa.json` a board is given, built from a placement and a posture.

Two axes and nothing else. **Placement** is Edge or Hub, and it decides one line: only the Hub
names where received files land. **Posture** is open, secure or control, and it decides how the
pair addresses and authenticates itself:

    open      no identity, so the 1-byte session address is hand-assigned and the two ends must
              carry the same number.
    secure    `identity_file` set, so the node generates and keeps an identity on first boot;
              first contact is addressed by `device_id[:4]` and the sid derives from
              `device_id[0]`. No `session_id`: one value registers a node instead of two.
    control   secure, plus a pinned control root on the node being commanded.

The rules below are refusals rather than defaults on purpose. Each one guards a state that
produces a board which looks provisioned and is not, and none of them is visible from the
outside: an open node with no `session_id` simply never hears its peer, a secure node with no
`identity_file` comes up with no `device_id` to register, and a node holding a control root
while its config says open can only ever do the refusing half of holding one.
"""

# The radio settings, spelled as the connector block itself spells them. `sf`, `freq`,
# `bandwidth` and `coding_rate` have to agree across the pair or the two boards do not hear
# each other, which is why one block feeds both configs.
DEFAULT_RF = {
    "sf": 7,
    "freq": 868,
    "bandwidth": 125,
    "coding_rate": 1,
    "tx_power": 14,
    "min_timeout": 0.5,
    "max_timeout": 12,
    "timeout_delta": 1,
    "debug": False,
}

RF_FIELDS = tuple(DEFAULT_RF)

# The name a board's config file is written under. `LoRa.json` named the one section of it that
# is actually about the radio, and the file carries the posture, the identity path, the control
# root, the result path and what the board is. Nodes still read the old name forever; nothing
# writes it any more.
CONFIG_NAME = "AlLoRa.json"

ROLES = ("edge", "hub")
POSTURES = ("open", "secure", "control")

# Which Connector class a board builds, keyed by the name written into `connector.driver`. The
# same vocabulary the wizard already offered as a flag, moved into the file so the board reads
# its own radio instead of inheriting whichever one its program happened to import.
DRIVERS = ("sx127x", "sx1262", "e5", "lopy4")

# Where an Edge keeps the files it has not delivered yet. Internal flash by default, so a board
# with no card works untouched and a board with one names its mount point instead.
DEFAULT_QUEUE_PATH = "Outbox"

IDENTITY_FILE = "identity.key"
CONTROL_ROOT_FILE = "control_root.key"

_DEFAULT_NAMES = {"edge": "S", "hub": "R"}


def merge_rf(overrides=None):
    """The default radio block with `overrides` applied, refusing any field the radio has not.

    A misspelled setting would otherwise be written to the board and ignored, leaving the pair
    running on values the operator believes they changed and no line anywhere saying so.
    """
    rf = dict(DEFAULT_RF)
    for key, value in (overrides or {}).items():
        if key not in DEFAULT_RF:
            raise ValueError(
                "'{}' is not a radio setting. The connector block holds: {}.".format(
                    key, ", ".join(RF_FIELDS)))
        if value is not None:
            rf[key] = value
    return rf


def build_lora_json(role, posture, driver="sx127x", name=None, rf=None, session_id=None,
                    on_site_root=False, result_path="Results", chunk_size=None,
                    queue_path=DEFAULT_QUEUE_PATH, debug=True):
    """The config file for one board.

    `driver` names the radio. It belongs in the file rather than in the program because a board
    whose radio is named by whichever module its `main.py` imported cannot be told, from the
    config an operator actually reads, that it is driving the wrong chip.

    `chunk_size` left as None omits the key, which is how a node is asked for the largest chunk
    its frames can carry. A number is still honoured and still clamped; what it cannot do is be
    a stale ceiling nobody chose.

    `on_site_root` is the named opt-in mode where the machine running Hub logic holds the
    signing half and is the authority, rather than a courier carrying artifacts minted by the
    operator. It is a separate argument from the posture because it changes the trust model
    rather than the wiring: choosing it means a compromised Hub can create commands and not
    only relay them.
    """
    if role not in ROLES:
        raise ValueError("role must be one of {}, not '{}'".format(", ".join(ROLES), role))
    if posture not in POSTURES:
        raise ValueError(
            "posture must be one of {}, not '{}'".format(", ".join(POSTURES), posture))
    if driver not in DRIVERS:
        raise ValueError(
            "driver must be one of {}, not '{}'. A radio this toolkit has no firmware target "
            "for is still a supported deployment: write its config by hand from an example, "
            "pass the Connector instance to the node, and skip the flash step.".format(
                ", ".join(DRIVERS), driver))
    if on_site_root:
        if posture != "control":
            raise ValueError(
                "on-site root only means something in the control posture: it decides who "
                "holds the signing half of a control root, and the other postures provision "
                "none.")
        if role != "hub":
            raise ValueError(
                "on-site root puts the signing half on the machine running Hub logic. An Edge "
                "given it holds the key that signs for the whole fleet, and refuses to boot.")

    config = {
        "name": name or _DEFAULT_NAMES[role],
        # What this board is. Presence of the key is the declaration: a bridge config carries
        # `adapter` instead, and neither is a value of the other. A config declaring nothing is
        # refused at boot rather than defaulted, because guessing here puts a node on the air in
        # a placement nobody chose.
        "node": role,
        "mesh_mode": False,
        "protocol_version": 3,
        "security_mode": "open" if posture == "open" else "secure",
        "debug": debug,
    }

    if posture == "open":
        if session_id is None:
            raise ValueError(
                "an open node needs a session_id, and both ends need the same one: open v3 "
                "data transfer is sid-addressed and there is no identity to derive the "
                "address from.")
        config["session_id"] = session_id
    else:
        if session_id is not None:
            raise ValueError(
                "a secure node derives its session id from device_id[0]; setting session_id "
                "would override the value the peer derives for it. Leave it unset.")
        # What makes the node generate and persist an identity on first boot. Without it the
        # node comes up secure-configured with no device_id, and there is nothing to register.
        config["identity_file"] = IDENTITY_FILE

    if posture == "control":
        # The commanded node pins the verifying half; the Hub pins the signing half only in
        # the on-site mode. Under the default the Hub names no root at all, because its
        # artifacts are minted by the operator and handed to ask_change_rf already signed.
        if role == "edge" or on_site_root:
            config["control_root_file"] = CONTROL_ROOT_FILE

    if chunk_size is not None:
        config["chunk_size"] = chunk_size

    if role == "hub":
        config["result_path"] = result_path
    else:
        config["queue_path"] = queue_path

    connector = merge_rf(rf)
    connector["driver"] = driver
    config["connector"] = connector
    return config

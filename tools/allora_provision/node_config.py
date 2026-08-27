"""The `LoRa.json` a board is given, built from a placement and a posture.

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

# The radio settings, spelled as `LoRa.json`'s own connector block spells them. `sf`, `freq`,
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

ROLES = ("edge", "hub")
POSTURES = ("open", "secure", "control")

IDENTITY_FILE = "identity.key"
CONTROL_ROOT_FILE = "control_root.key"

# The example pair's numbers, kept because they are what the bench has actually run.
_DEFAULT_CHUNK_SIZE = 200
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


def build_lora_json(role, posture, name=None, rf=None, session_id=None,
                    on_site_root=False, result_path="Results", chunk_size=_DEFAULT_CHUNK_SIZE,
                    debug=True):
    """The config file for one board.

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
        "chunk_size": chunk_size,
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

    if role == "hub":
        config["result_path"] = result_path

    config["connector"] = merge_rf(rf)
    return config

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

# Which radios read their pins out of the connector block, and which carry them somewhere else.
# `SX1262_connector` and `E5_connector` both read pins from the config and both fall back to a
# map for a board nobody named, which is the defect this whole table exists to close. `sx127x`
# takes its pins from its own driver's board file and a `lopy4` has one radio on one set of
# pins, so neither has anything to read.
#
# The distinction decides what an attachment row has to exist for. A pair whose radio reads
# pins and has no row cannot be provisioned; a pair whose radio reads none is complete without
# one.
PIN_READING_RADIOS = ("sx1262", "e5")

# What each board is made of. **A board does not imply its radio**: the bench pair is two T3-S3s
# carrying different chips, so a table that mapped a board to one radio would be wrong about
# exactly the deployment it was written for.
#
# A row says three things. `has` is the radios soldered into that product, one per unit,
# whichever one was bought. `hosts` is the radios that arrive as a module on a header, which is
# a different kind of fact: an E5 is an AT modem on a UART and the same module moves between
# boards. `pins` is where a radio and this board actually meet, and it is a fact about the pair
# rather than about either one, since the same SX1262 sits on clk 5 here and on clk 9 on a
# Heltec LoRa32 V3.
#
# **No row is written for hardware that is not on the desk.** A pin map read off a datasheet is
# a guess wearing a table's clothes, and a guess is what produces a config that reports
# provisioned at every step and cannot find its own chip.
BOARD_TABLE = {
    # Read from LilyGo's `utilities.h` and confirmed on the bench: the radio comes up on these
    # seven pins and needs no others.
    "t3s3": {
        "provisioning": "flashed",
        "has": ("sx127x", "sx1262"),
        "hosts": ("e5",),
        "pins": {
            "sx1262": {"clk": 5, "mosi": 6, "miso": 3, "cs": 7, "rst": 8, "irq": 33,
                       "gpio": 34},
        },
    },
}

# One wiring row can serve two products. The E-Paper variant wires its radio identically to the
# plain T3-S3 and differs only in its screen, which is not a radio fact.
#
# **The registry records the name the operator gave, not the row it resolved to.** When a
# revision moves a pin, a registry full of `t3s3` cannot say which nodes are affected and one
# that kept the operator's word can.
BOARD_ALIASES = {"t3s3-epaper": "t3s3"}

BOARDS = tuple(BOARD_TABLE) + tuple(BOARD_ALIASES)

# The board every firmware target in this repository is built for. A default is right here and
# wrong in the table itself: one board is what this toolkit can flash, and a second one arrives
# as a row rather than as a guess.
DEFAULT_BOARD = "t3s3"


def board_row(board):
    """The wiring row for `board`, resolving an alias, or raise saying it is not in the table."""
    name = BOARD_ALIASES.get(board, board)
    if name not in BOARD_TABLE:
        raise ValueError(
            "board must be one of {}, not '{}'. The board is what decides where the radio is "
            "wired, and one this toolkit has no pin map for cannot be provisioned by guessing: "
            "a guessed map writes a config that reports provisioned and cannot find its chip. "
            "Write that board's config by hand from an example, naming the pins in the "
            "connector block.".format(", ".join(sorted(BOARDS)), board))
    return BOARD_TABLE[name]


def radios_for(board):
    """Every radio this board can carry, soldered or hosted, in the order the table names them."""
    row = board_row(board)
    return tuple(row["has"]) + tuple(row["hosts"])


def check_pair(board, radio):
    """Raise unless this board can carry this radio and somebody has recorded where.

    **Two refusals, and they are not the same refusal.** A board that cannot carry the radio at
    all is refused permanently: a bare SPI transceiver is part of a board's design and is never
    something plugged into a header, so no amount of bench work makes the combination real. A
    board that can carry it and has no attachment row is refused for now, and the message says
    so, because the missing thing is a measurement somebody can go and take.
    """
    row = board_row(board)
    if radio not in radios_for(board):
        raise ValueError(
            "a {} does not carry an {}. That board comes with {} soldered in{}, and a bare "
            "transceiver is part of a board's design rather than something added to one, so "
            "this pair is not a deployment waiting on a pin map.".format(
                board, radio, " or ".join(row["has"]),
                " and can host {}".format(" or ".join(row["hosts"])) if row["hosts"] else ""))
    if radio in PIN_READING_RADIOS and radio not in row["pins"]:
        raise ValueError(
            "an {} on a {} is a real combination and nobody has recorded which pins it lands "
            "on. The connector reads its pins from the config and falls back to another board's "
            "map when they are absent, so provisioning this pair by guessing would write a "
            "config that reports provisioned and cannot find its chip. Put the module on the "
            "bench, note the pins, and add them to the board table.".format(radio, board))


def attachment(board, radio):
    """Where this radio meets this board, for the radios that read that out of their config."""
    check_pair(board, radio)
    return dict(board_row(board)["pins"].get(radio, {}))

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


def _check_placement(role, posture, driver, board, on_site_root):
    """Refuse a combination before a single key is written.

    All of these produce a board that looks provisioned and is not, so they are checked as a
    group and before the config exists: a half-built dict handed back with a raise beside it is
    a thing a caller can be tempted to use.
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
    check_pair(board, driver)
    if not on_site_root:
        return
    if posture != "control":
        raise ValueError(
            "on-site root only means something in the control posture: it decides who holds "
            "the signing half of a control root, and the other postures provision none.")
    if role != "hub":
        raise ValueError(
            "on-site root puts the signing half on the machine running Hub logic. An Edge "
            "given it holds the key that signs for the whole fleet, and refuses to boot.")


def _addressing_keys(posture, session_id):
    """How this node is addressed: a hand-assigned number, or an identity it generates."""
    if posture == "open":
        if session_id is None:
            raise ValueError(
                "an open node needs a session_id, and both ends need the same one: open v3 "
                "data transfer is sid-addressed and there is no identity to derive the "
                "address from.")
        return {"session_id": session_id}
    if session_id is not None:
        raise ValueError(
            "a secure node derives its session id from device_id[0]; setting session_id would "
            "override the value the peer derives for it. Leave it unset.")
    # What makes the node generate and persist an identity on first boot. Without it the node
    # comes up secure-configured with no device_id, and there is nothing to register.
    return {"identity_file": IDENTITY_FILE}


def build_lora_json(role, posture, driver="sx127x", name=None, rf=None, session_id=None,
                    on_site_root=False, result_path="Results", chunk_size=None,
                    queue_path=DEFAULT_QUEUE_PATH, debug=True, board=DEFAULT_BOARD):
    """The config file for one board.

    `driver` names the radio. It belongs in the file rather than in the program because a board
    whose radio is named by whichever module its `main.py` imported cannot be told, from the
    config an operator actually reads, that it is driving the wrong chip.

    `board` names what the radio is soldered to, which is the only thing that knows where it is
    wired. Naming a radio is not enough on its own: two boards carrying the same chip put it on
    different pins, and the connector cannot tell that it was handed the wrong map.

    **No `device` section is written.** That key makes the node build a board object at boot,
    and a board file that does not match the hardware raises inside a peripheral driver before
    any radio code runs. Peripherals are something an operator turns on deliberately; what this
    writes is the radio and nothing else.

    `chunk_size` left as None omits the key, which is how a node is asked for the largest chunk
    its frames can carry. A number is still honoured and still clamped; what it cannot do is be
    a stale ceiling nobody chose.

    `on_site_root` is the named opt-in mode where the machine running Hub logic holds the
    signing half and is the authority, rather than a courier carrying artifacts minted by the
    operator. It is a separate argument from the posture because it changes the trust model
    rather than the wiring: choosing it means a compromised Hub can create commands and not
    only relay them.
    """
    _check_placement(role, posture, driver, board, on_site_root)

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

    config.update(_addressing_keys(posture, session_id))

    # The commanded node pins the verifying half; the Hub pins the signing half only in the
    # on-site mode. Under the default the Hub names no root at all, because its artifacts are
    # minted by the operator and handed to ask_change_rf already signed.
    if posture == "control" and (role == "edge" or on_site_root):
        config["control_root_file"] = CONTROL_ROOT_FILE

    if chunk_size is not None:
        config["chunk_size"] = chunk_size

    if role == "hub":
        config["result_path"] = result_path
    else:
        config["queue_path"] = queue_path

    connector = merge_rf(rf)
    connector["driver"] = driver
    # Where this radio is wired on this board, for the radios that read it from here. Left out,
    # the connector silently uses another board's map and the chip is never found.
    connector.update(attachment(board, driver))
    config["connector"] = connector
    return config

"""The plan: what a provisioning run intends to do, written down before it does any of it.

The plan is the unit. `setup` writes one as it asks its questions and then applies it; `apply`
runs one somebody else wrote, a website included. Same file, same engine, and the interactive
path gains an artefact the operator can keep, diff, re-run and send to somebody else.

**A plan names boards by MAC, never by port.** The port is not an identity: on native USB it
re-enumerates on every hard reset, so a plan written on Monday and re-run on Tuesday would aim
at whichever board happened to land on that path. The MAC survives a flash and names one
physical board, which is why the wizard already offers boards by it. `ports_for` is where the
two are matched up, once, at the start of a run.

**A plan says scratch or extend, and that is its first field for a reason.** Extend is adding
an Edge, replacing a broken node, or swapping a Hub into a deployment that is already in the
field. Not asking the question is what let a run flash into a non-empty fleet, orphan a record
and append a phantom the Hub then spent a listening window polling every cycle.

**Each node names its own board, radio and firmware; the RF settings stay run-level.** Which
chip a node has and where it is attached is a fact about that node: the bench pair is an SX127x
Hub and an SX1262 Edge, and a run-level radio cannot describe it. How the radio is tuned is a
fact about the deployment, because both ends have to agree on `sf`, `freq` and `bandwidth` or
they do not hear each other, and a per-node setting there is a way to build a fleet that cannot
talk.

**A node may be described before the board that will be it exists.** Board, radio, role, name,
posture and RF are design-time facts, which somebody planning a deployment knows. Which
physical unit fills the slot is a bench-time fact, which they do not. So an entry may carry no
MAC: `validate` accepts that, and the refusal moves to the only moment it matters, which is
`apply`, where something is about to be flashed.

This module holds the document and nothing else: no board is touched here, no fleet is read.
That is what lets a plan be checked, printed and diffed before anything is plugged in.
"""
import json
import os

from tools.allora_provision.node_config import DRIVERS, check_pair

VERSION = 1

# What this run is doing to the deployment named by `fleet`.
#
#   scratch   nothing is there yet, or the operator is deliberately starting a new one. Both
#             ends are provisioned and the Hub is built from nothing.
#   extend    the deployment exists and stays running. New nodes are added to it, and the Hub
#             gets its roster updated rather than rebuilt.
MODES = ("scratch", "extend")

# The radio settings a plan carries. The rest of the connector block has never needed changing
# at the bench and the non-interactive commands take flags for all of it; a plan that had to
# spell nine values would be a config file with a different name.
RF_KEYS = ("sf", "freq", "bandwidth")

_REQUIRED = ("mode", "fleet", "posture", "rf", "edges", "hub")

# What one node entry holds. `mac` is the board that will be it, and is the one field a plan may
# leave empty. `firmware` empty means this node keeps what it is running, which is a legitimate
# answer and the only safe one for a target this machine holds no image for.
_ENTRY_KEYS = ("mac", "name", "board", "radio", "firmware")


def _entry(node):
    """One node entry, carrying its own hardware and nothing the run-level fields already say."""
    return {key: node.get(key) for key in _ENTRY_KEYS}


def build(mode, fleet, posture, rf, edges, hub, session_id=None):
    """A plan document, in the order it reads best rather than the order it was built.

    `edges` and `hub` are node entries: a MAC, a name, a board, a radio and a firmware image.
    A name of None means the phase picks the default for the role, which is only ever right
    when the fleet holds no node under it. A MAC of None is a slot nobody has bound yet.
    """
    return {
        "version": VERSION,
        "mode": mode,
        "fleet": fleet,
        "posture": posture,
        "session_id": session_id,
        "rf": {key: rf[key] for key in RF_KEYS if key in rf},
        "edges": [_entry(e) for e in edges],
        "hub": _entry(hub),
    }


def validate(doc):
    """Return `doc` if it is a plan this toolkit can run, or raise saying what is wrong.

    Checked as a document, before a board is opened, because the alternative is a run that
    gets as far as flashing one board and then finds the second entry has no MAC.
    """
    if not isinstance(doc, dict):
        raise ValueError("a plan is a JSON object; this file holds {}".format(
            type(doc).__name__))

    version = doc.get("version")
    if version != VERSION:
        raise ValueError(
            "this plan says version {}, and this toolkit writes and runs version {}. A plan is "
            "not a config file to hand-upgrade: write a fresh one with `provision "
            "setup`.".format(version, VERSION))

    missing = [key for key in _REQUIRED if key not in doc]
    if missing:
        raise ValueError("this plan is missing: {}".format(", ".join(missing)))

    if doc["mode"] not in MODES:
        raise ValueError("mode must be one of {}, not '{}'".format(
            ", ".join(MODES), doc["mode"]))

    edges = doc["edges"]
    if not isinstance(edges, list) or not edges:
        raise ValueError(
            "a plan provisions at least one Edge. A plan that names only a Hub has nothing for "
            "it to poll, and the Hub phase refuses a fleet with no Edge in it.")

    for index, entry in enumerate([doc["hub"]] + list(edges)):
        where = "the hub" if index == 0 else "edge {}".format(index)
        if not isinstance(entry, dict):
            raise ValueError("{} is not a node entry; this plan holds {}".format(
                where, type(entry).__name__))
        _validate_hardware(entry, where)

    bound = [entry["mac"] for entry in _entries(doc) if entry.get("mac")]
    repeated = sorted({mac for mac in bound if bound.count(mac) > 1})
    if repeated:
        raise ValueError(
            "the same board is named twice in this plan ({}). One board is one node: a board "
            "cannot be both an Edge and the Hub, and a second Edge needs a second "
            "board.".format(", ".join(repeated)))

    names = [e.get("name") for e in edges if e.get("name")]
    clashing = sorted({name for name in names if names.count(name) > 1})
    if clashing:
        raise ValueError(
            "two Edges in this plan are called {}. In a fleet one name is one node: the "
            "registry keys a node's slot by its role and its name, so the second registration "
            "would update the first one's record instead of adding its own.".format(
                ", ".join(clashing)))
    return doc


def _entries(doc):
    """Every node this plan describes, Hub first, in the order the run reaches them."""
    return [doc["hub"]] + list(doc["edges"])


def _validate_hardware(entry, where):
    """What one node is made of, checked as a document before a board is opened.

    The radio is checked against the board rather than on its own, because the pair is what
    decides whether a config can be written: a board that cannot carry the radio is one
    refusal, and a board that can carry it with nobody having recorded the pins is another.
    """
    board, radio = entry.get("board"), entry.get("radio")
    if not board or not radio:
        raise ValueError(
            "{} names no {}. A node is a board and a radio, and neither follows from the other: "
            "two boards of the same model carry different chips, so a plan that named only one "
            "of them would describe half of this node.{}".format(
                where, "board" if not board else "radio",
                # A plan written when the radio was one field for the whole run reads exactly
                # like this, and it is likelier than a hand-written mistake.
                " A plan that names one radio for the whole run predates this: write a fresh "
                "one with `provision setup`." if not board and not radio else ""))
    if radio not in DRIVERS:
        raise ValueError("{} names radio '{}', and this toolkit knows {}.".format(
            where, radio, ", ".join(DRIVERS)))
    try:
        check_pair(board, radio)
    except ValueError as e:
        raise ValueError("{}: {}".format(where, e))


def unbound(doc):
    """The slots this plan describes and no board has been assigned to yet."""
    absent = []
    for index, entry in enumerate(_entries(doc)):
        if not entry.get("mac"):
            absent.append("the hub" if index == 0 else
                          (entry.get("name") or "edge {}".format(index)))
    return absent


def require_bound(doc):
    """Return `doc` if every slot names a board, or raise saying which ones do not.

    The refusal a design becomes a run at. A plan may be written with no hardware in the room,
    which is what lets a deployment be designed away from the bench; what may never happen is a
    flash aimed at a slot nobody has said which board fills.
    """
    absent = unbound(doc)
    if absent:
        raise ValueError(
            "this plan describes {} that no board is assigned to yet ({}). A plan can be "
            "written before the hardware is on the desk, and running one cannot: `provision "
            "setup` binds a design to the boards answering, and `provision ports` prints the "
            "MAC of each one.".format(
                "a slot" if len(absent) == 1 else "slots", ", ".join(absent)))
    return doc


def read(path):
    """The plan at `path`, validated. Raises with the path in it when it cannot be read."""
    try:
        with open(path, "r") as f:
            doc = json.load(f)
    except (IOError, OSError) as e:
        raise ValueError("could not read the plan at {}: {}".format(path, e))
    except ValueError as e:
        raise ValueError("{} is not valid JSON: {}".format(path, e))
    return validate(doc)


def write(path, doc):
    """Write a plan, through a rename, and hand back the path.

    Committed the way the fleet's own files are, because a plan truncated by an interrupted
    write reads back as a plan that is missing fields rather than as a plan that is not there,
    and the second is much easier to act on.
    """
    validate(doc)
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    temp = path + ".tmp"
    with open(temp, "w") as f:
        f.write(json.dumps(doc, indent=2) + "\n")
    os.replace(temp, path)
    return path


def macs(doc):
    """Every board this plan touches, Hub first, in the order the run reaches them."""
    return [entry["mac"] for entry in _entries(doc) if entry.get("mac")]


def ports_for(doc, boards):
    """Match the plan's MACs to the ports they are answering on right now.

    `boards` is what `look_for_boards` returns: `{"port", "mac"}` for every board answering a
    REPL. Every MAC has to be found before anything runs, so a plan naming a board nobody
    plugged in fails at the desk rather than halfway through, with one board provisioned and
    the other still holding whatever it held.

    An unbound plan is refused here too, and for the same reason one step earlier: a design
    nobody has bound names no board to look for, so matching it against the desk would report
    every slot as present and flash nothing.
    """
    require_bound(doc)
    answering = {}
    for board in boards:
        if board.get("mac"):
            answering[board["mac"]] = board["port"]

    ports = {}
    absent = []
    for mac in macs(doc):
        if mac in answering:
            ports[mac] = answering[mac]
        else:
            absent.append(mac)
    if absent:
        raise ValueError(
            "this plan names {} that {} not answering ({}). {} answering right now: {}. A plan "
            "addresses boards by MAC, so plug in the ones it names, or write a new plan for "
            "the boards on this desk.".format(
                "a board" if len(absent) == 1 else "boards",
                "is" if len(absent) == 1 else "are",
                ", ".join(absent),
                "Nothing is" if not answering else
                ("1 board is" if len(answering) == 1 else
                 "{} boards are".format(len(answering))),
                ", ".join("{} on {}".format(m, p) for m, p in sorted(answering.items()))
                or "none"))
    return ports

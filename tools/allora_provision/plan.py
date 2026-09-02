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

This module holds the document and nothing else: no board is touched here, no fleet is read.
That is what lets a plan be checked, printed and diffed before anything is plugged in.
"""
import json
import os

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

_REQUIRED = ("mode", "fleet", "posture", "radio", "rf", "edges", "hub")


def build(mode, fleet, posture, radio, rf, edges, hub, firmware=None, session_id=None):
    """A plan document, in the order it reads best rather than the order it was built.

    `edges` and `hub` are `{"mac": ..., "name": ...}`. A name of None means the phase picks the
    default for the role, which is only ever right when the fleet holds no node under it.
    """
    return {
        "version": VERSION,
        "mode": mode,
        "fleet": fleet,
        "posture": posture,
        "session_id": session_id,
        "radio": radio,
        "rf": {key: rf[key] for key in RF_KEYS if key in rf},
        "firmware": firmware,
        "edges": [{"mac": e["mac"], "name": e.get("name")} for e in edges],
        "hub": {"mac": hub["mac"], "name": hub.get("name")},
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
        if not isinstance(entry, dict) or not entry.get("mac"):
            raise ValueError(
                "{} names no MAC. A plan addresses boards by MAC and not by port, because the "
                "port changes on every hard reset; `provision ports` prints the MAC of every "
                "board answering.".format(where))

    macs = [doc["hub"]["mac"]] + [e["mac"] for e in edges]
    repeated = sorted({mac for mac in macs if macs.count(mac) > 1})
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
    return [doc["hub"]["mac"]] + [edge["mac"] for edge in doc["edges"]]


def ports_for(doc, boards):
    """Match the plan's MACs to the ports they are answering on right now.

    `boards` is what `look_for_boards` returns: `{"port", "mac"}` for every board answering a
    REPL. Every MAC has to be found before anything runs, so a plan naming a board nobody
    plugged in fails at the desk rather than halfway through, with one board provisioned and
    the other still holding whatever it held.
    """
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

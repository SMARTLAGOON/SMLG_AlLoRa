"""`provision setup`: the one interactive command, for a person at the bench.

It asks everything first, writes the answers as a plan, and runs it with the same engine as
`apply`. It lists the boards so you pick which is which, asks scratch or extend, and asks for a
RESET press if a new board stays silent. Questions go through an injected `Prompt` for the tests.
"""
import os
import sys

from AlLoRa.Digital_Endpoint import label_for_config
from tools.allora_provision import doctor as doctor_module
from tools.allora_provision import plan as plan_module
from tools.allora_provision.board import Board, BoardError, discover_boards
from tools.allora_provision.fleet import Fleet
from tools.allora_provision.node_config import (BOARD_ALIASES, BOARD_TABLE, DEFAULT_BOARD,
                                                DEFAULT_NAMES, DEFAULT_QUEUE_PATH,
                                                DEFAULT_RESULT_PATH, DEFAULT_RF, POSTURES,
                                                check_pair, radios_for)
from tools.allora_provision.result import OK, SKIPPED
from tools.allora_provision.steps import (PAYLOAD_LEN, provision_edge, provision_hub,
                                           start_programs, update_hub_roster, verify_pair)

# How every command the wizard prints is typed. There is no installed `provision` command.
CLI = "python3 tools/provision.py"

# Where a build lands. Searched rather than named so the operator never types a path that
# changes with every CI run.
FIRMWARE_DIR = "firmware"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# What each posture means, in the words somebody choosing between them needs. The full
# consequences of `control` are printed by the phase that provisions it; this is the sentence
# that gets the choice right.
_POSTURE_HELP = {
    "open": "no identity, no encryption. Each node is addressed by its MAC.",
    "secure": "each node gets its own identity, and every frame is encrypted and checked.",
    "control": "secure, plus signed commands: the Edge acts only on a command signed with "
               "this deployment's key.",
}

_RADIO_HELP = {
    "sx127x": "SX1276 transceiver",
    "sx1262": "SX1262 transceiver",
    "e5": "Wio-E5 modem",
    "lopy4": "Pycom LoPy4",
}

# What the operator calls the thing in their hand. The table's own name is a wiring row and the
# two variants here share one, so a board is offered under the name printed on it: the person
# at the bench is answering "which of these am I holding", not "which pin map applies".
_BOARD_HELP = {
    "t3s3": "LilyGo T3-S3",
    "t3s3-epaper": "LilyGo T3-S3 E-Paper",
}

# The radio settings worth asking about. The rest of the connector block has defaults that have
# never needed changing at the bench, and the non-interactive commands take flags for all of
# them; a wizard that asked nine questions to get to a plan would not be one.
_ASKED_RF = (("sf", "spreading factor"), ("freq", "frequency, MHz"),
             ("bandwidth", "bandwidth, kHz"))


class Prompt:
    """The conversation half of the wizard, with its reader and its writer injected.

    Stopping is a first-class answer. Ctrl-C and Ctrl-D both mean the same thing here, and both
    have to leave through the same door as any other refusal: a `BoardError` the command turns
    into one line, rather than a traceback in front of somebody who was only trying to quit.
    """

    def __init__(self, read=None, out=None):
        self._read = read if read is not None else input
        self._out = out if out is not None else sys.stdout

    # --- saying things ------------------------------------------------------------------

    def say(self, line=""):
        self._out.write(line + "\n")
        self._out.flush()

    def stage(self, index, total, title):
        self.say("\n[{}/{}] {}".format(index, total, title))

    def bullet(self, line):
        self.say("      " + line)

    # --- asking things ------------------------------------------------------------------

    def _ask(self, question, default=None):
        suffix = " (Enter for {}): ".format(default) if default is not None else ": "
        self._out.write("  " + question + suffix)
        self._out.flush()
        try:
            answer = self._read("")
        except (EOFError, KeyboardInterrupt):
            raise BoardError("stopped at the keyboard. Nothing was changed on any board.")
        if answer is None:
            raise BoardError("stopped at the keyboard. Nothing was changed on any board.")
        answer = answer.strip()
        return answer if answer else ("" if default is None else str(default))

    def text(self, question, default=None):
        while True:
            answer = self._ask(question, default)
            if answer:
                return answer
            self.bullet("an answer is needed here.")

    def number(self, question, default=None, low=None, high=None):
        while True:
            raw = self._ask(question, default)
            try:
                value = int(raw)
            except ValueError:
                self.bullet("that is not a number.")
                continue
            if low is not None and value < low:
                self.bullet("the smallest this can be is {}.".format(low))
                continue
            if high is not None and value > high:
                self.bullet("the largest this can be is {}.".format(high))
                continue
            return value

    def choose(self, question, options, default=0):
        """Pick one of `options`, a list of `(value, label)`. Returns the value.

        Numbered rather than typed, because every list here is a list of things the operator
        cannot spell from memory: a MAC, a firmware path, a posture name.
        """
        self.say("")
        self.say("  " + question)
        for index, (_, label) in enumerate(options, start=1):
            marker = " (default)" if index - 1 == default else ""
            self.say("    {}) {}{}".format(index, label, marker))
        index = self.number("choose 1-{}".format(len(options)), default=default + 1,
                            low=1, high=len(options))
        return options[index - 1][0]

    def yes(self, question, default=True):
        marker = "Y/n" if default else "y/N"
        while True:
            answer = self._ask(question, marker).strip().lower()
            if answer == marker.lower():      # the operator pressed Enter and took the default
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.bullet("answer y or n.")

    def pause(self, message):
        self._out.write("  " + message + " ")
        self._out.flush()
        try:
            self._read("")
        except (EOFError, KeyboardInterrupt):
            raise BoardError("stopped at the keyboard. Nothing was changed on any board.")


def firmware_images(root=None, limit=None):
    """The firmware builds on this machine, newest first.

    Newest first because the answer for any one target is nearly always the build that was just
    downloaded. Unlimited by default: the list is matched against a target rather than shown as
    a menu, and a cap would hide the only image for one radio behind six newer ones for the
    other, which on a mixed pair is the whole problem.
    """
    base = os.path.join(root or _REPO_ROOT, FIRMWARE_DIR)
    found = []
    for directory, _, names in os.walk(base):
        for name in names:
            if name.endswith(".bin"):
                path = os.path.join(directory, name)
                found.append((os.path.getmtime(path), path))
    found.sort(reverse=True)
    return [path for _, path in found[:limit]] if limit else [path for _, path in found]


def target_of(board, radio):
    """The firmware target one board and one radio need, in the name CI builds it under.

    A target is exactly a board and a radio, so it is derived here rather than stored: an alias
    resolves to its wiring row first, because the image is built for the wiring and the E-Paper
    variant runs the plain board's build.
    """
    return "{}-{}".format(BOARD_ALIASES.get(board, board), radio)


def image_for(target, images):
    """The newest build for this target, or None if this machine holds none.

    Matched on the filename because that is where the target already lives: CI names the
    artifact `AlLoRa-<target>-firmware.bin`. None is a real answer and not a failure. It means
    this node keeps what it is running, which is the only safe thing to do with a board whose
    image is not on this machine: the alternative is flashing it with another radio's build,
    which is a board that reports provisioned at every step and never finds its chip.
    """
    for path in images:
        if target in os.path.basename(path):
            return path
    return None


def hardware_options():
    """Every board and radio pair this toolkit can provision, in the order it offers them.

    Built from the board table rather than listed, so a pair nobody has recorded the pins for
    is not offered by the wizard and not silently accepted either: it is refused by the same
    check that refuses it in a plan somebody else wrote.
    """
    options = []
    for board in list(BOARD_TABLE) + list(BOARD_ALIASES):
        for radio in radios_for(board):
            try:
                check_pair(board, radio)
            except ValueError:
                continue
            options.append((board, radio))
    return options


def ask_hardware(prompt, board, default=None):
    """What one board is: the model in the operator's hand and the chip on it.

    Asked per board, and asked at all, because a board does not imply its radio. The bench pair
    is two T3-S3s carrying different chips, so one question for the run would describe one of
    them wrongly and the wizard would write a config for a radio that is not there.
    """
    options = hardware_options()
    labels = [(pair, "{:<12} {:<7} {}".format(
        pair[0], pair[1], _BOARD_HELP.get(pair[0], _RADIO_HELP[pair[1]])))
        for pair in options]
    index = options.index(default) if default in options else 0
    return prompt.choose("What is {}?".format(_label(board)), labels, default=index)


def look_for_boards(runner=None):
    """Every board answering right now, with the MAC that says which one it is.

    Reading a MAC stops the board's program, so whoever calls this owes each board a restart:
    `restart_the_rest` pays it for every board a plan did not take over.
    """
    boards = []
    for port in discover_boards(runner=runner):
        boards.append({"port": port, "mac": Board(port, runner=runner).mac()})
    return boards


def restart_the_rest(boards, owned, result, runner=None):
    """Restart every board that was only looked at, however the command ends.

    Found on the bench, 2026-10-21: a run stopped at its first question left three boards at
    the prompt, a deployment off the air until each was reset. The boards a plan names are not
    restarted here: `run_plan` starts the ones it finished and leaves stopped the ones it may
    have left half written.
    """
    start_programs([Board(board["port"], runner=runner) for board in boards
                    if board["mac"] is None or board["mac"] not in owned], result)


def _label(board):
    return "{}  on {}".format(board["mac"] or "MAC unknown", board["port"])


def _find(boards, mac):
    """The board answering under this MAC, or a placeholder that says it is not here."""
    for board in boards:
        if board.get("mac") == mac:
            return board
    return {"mac": mac, "port": "not plugged in"}


def deployment_defaults(entries):
    """What this fleet already is, so extending it does not start from the built-in defaults.

    An operator extending a deployment is not choosing a posture and a radio, they are naming
    the one their nodes are already on. Offering the built-in defaults instead makes taking
    every default the way to break the deployment, which is the wrong shape for a wizard whose
    every question can be answered with Enter.
    """
    defaults = {"posture": "secure", "radio": "sx127x", "board": DEFAULT_BOARD, "rf": {}}
    for entry in entries:
        if entry.get("posture"):
            defaults["posture"] = entry["posture"]
        if entry.get("radio"):
            defaults["radio"] = entry["radio"]
        if entry.get("board"):
            defaults["board"] = entry["board"]
        if entry.get("role") != "edge":
            continue
        block = entry.get("connector") or {}
        stated = {key: block[key] for key in plan_module.RF_KEYS
                  if block.get(key) is not None}
        if stated:
            defaults["rf"] = stated
    return defaults


def free_edge_names(entries, count):
    """`count` Edge names this fleet does not already hold.

    A name is a slot: the registry keys a node by its role and its name, so a second Edge
    taking the first one's default name updates that record instead of adding its own, and the
    Hub is then handed a roster naming one node where the operator provisioned two.
    """
    taken = set(e.get("name") for e in entries if e.get("role") == "edge")
    names, index = [], 1
    while len(names) < count:
        index += 1
        candidate = "S{}".format(index)
        if candidate not in taken:
            names.append(candidate)
            taken.add(candidate)
    return names


def ask_edge_names(prompt, chosen, entries):
    """Whether each board being provisioned is a new node or replaces one already registered.

    The question that separates two of the three things extend covers. A replacement board is
    not a new node: it holds a fingerprint nothing has heard of, and taking a new name would
    leave the fleet and the Hub's roster both carrying the record of a board that is dead. The
    Hub then spends a listening window every cycle polling a node nobody holds.

    A name is the answer because a name is the slot. Registering under the old one updates that
    record rather than adding beside it, and the roster row keyed to the old fingerprint is
    replaced by the row keyed to the new one.
    """
    registered = [e for e in entries if e.get("role") == "edge" and e.get("name")]
    fresh = free_edge_names(entries, len(chosen))
    if not registered:
        return fresh

    names = []
    for index, board in enumerate(chosen):
        options = [(fresh[index], "a new node, registered as {}".format(fresh[index]))]
        options += [(entry["name"],
                     "replacing {}, whose board is gone: it keeps the name and the "
                     "slot".format(entry["name"]))
                    for entry in registered]
        names.append(prompt.choose(
            "Is {} a new node, or a replacement?".format(_label(board)), options, default=0))
    return names


def _registered_hardware(entries, role):
    """What the fleet says this role is made of, for a run that is not re-provisioning it.

    A fleet written before a node's board was recorded says only its radio, so the board falls
    back to the one this toolkit flashes. That is the same assumption the run made when it
    wrote the entry, rather than a new guess.
    """
    for entry in reversed(entries):
        if entry.get("role") == role and entry.get("radio"):
            return (entry.get("board") or DEFAULT_BOARD, entry["radio"])
    return (DEFAULT_BOARD, "sx127x")


def ask_mode(prompt, entries):
    """Scratch or extend, asked only when there is something to extend.

    The question that stops a run walking into a deployment it did not know was there: flashing
    into a non-empty fleet orphans a record, appends a phantom, and leaves the Hub polling a
    node nobody holds. On an empty fleet there is nothing to extend, so asking would be noise.
    """
    if not entries:
        return "scratch"
    prompt.say("")
    prompt.say("  This fleet already holds {} node(s):".format(len(entries)))
    for entry in entries:
        prompt.bullet("{:<6} {:<5} {:<8} {}".format(
            entry.get("name", "?"), entry.get("role", "?"), entry.get("posture", "?"),
            entry.get("device_id", "") or "(no identity)"))
    return prompt.choose(
        "Is this deployment being extended, or is this a new one?",
        [("extend", "extend it   add nodes; the ones above keep running, untouched"),
         ("scratch", "start a new one  in a different directory; this one is left alone")],
        default=0)


def ask_posture(prompt, known, entries, extending):
    """Which posture this run provisions, and what it means for the nodes it does not touch."""
    posture = prompt.choose(
        "Security posture?",
        [(name, "{:<8} {}".format(name, _POSTURE_HELP[name])) for name in POSTURES],
        default=list(POSTURES).index(known["posture"] if extending else "secure"))
    if not extending or posture == known["posture"]:
        return posture

    # Warned and guided, never refused. A posture is what a node was provisioned with, so
    # changing it means reflashing every node in the deployment; a tool that refused would send
    # the operator to do exactly that by hand, one board at a time, which is the outcome the
    # refusal was trying to prevent.
    prompt.say("")
    prompt.bullet("this deployment is {}, and you have asked for {}.".format(
        known["posture"], posture))
    prompt.bullet("a posture is what a node was provisioned with, not a setting it can be "
                  "told. Every node already in this fleet keeps the old one until it is "
                  "provisioned again, and a {} node and a {} node cannot talk to each "
                  "other.".format(known["posture"], posture))
    prompt.bullet("this run provisions the nodes you pick below. The rest stay {} and stop "
                  "being part of the working deployment until you come back for "
                  "them:".format(known["posture"]))
    for entry in entries:
        prompt.bullet("  {:<6} {:<5} {}".format(
            entry.get("name", "?"), entry.get("role", "?"), entry.get("posture", "?")))
    if not prompt.yes("Go on?", default=False):
        raise BoardError(
            "nothing was changed. Run `{} setup` again and keep the {} posture to add "
            "to this deployment as it is.".format(CLI, known["posture"]))
    return posture


def _registered_as(entries, mac):
    """The newest record this fleet holds for the board with this MAC, or None."""
    return next((e for e in reversed(entries) if mac and e.get("mac") == mac), None)


def _registered_hub_mac(entries):
    return next((e.get("mac") for e in reversed(entries)
                 if e.get("role") == "hub" and e.get("mac")), None)


def ask_edges(prompt, boards, extending, entries=()):
    """How many Edges this run provisions and which boards they are, and what is left over.

    On an extend run the boards the fleet already holds are marked, the default is one it does
    not, and picking a registered Edge is asked twice. Found on the bench, 2026-10-21: the first
    port was the fleet's own Hub, and Enter at every question turned it into an Edge. The Hub
    itself is refused outright: extending writes the roster onto that board, so with it taken
    as an Edge the Hub question could only stop the run after every answer was given.
    """
    most = len(boards) - 1
    edges = 1 if most <= 1 else prompt.number(
        "How many Edge nodes {}?".format(
            "are you adding" if extending else "in this deployment"),
        default=1, low=1, high=most)
    hub_mac = _registered_hub_mac(entries)

    def label(board):
        entry = _registered_as(entries, board["mac"]) if extending else None
        if entry is None:
            return _label(board)
        return "{}   (this fleet's {} {})".format(
            _label(board), "Hub" if entry.get("role") == "hub" else "Edge", entry.get("name"))

    def confirmed(board):
        entry = _registered_as(entries, board["mac"]) if extending else None
        if entry is None:
            return True
        prompt.say("")
        if board["mac"] == hub_mac:
            prompt.bullet("{} stays this fleet's Hub {}: adding nodes writes their rows onto "
                          "it. Pick another board.".format(board["mac"], entry.get("name")))
            return False
        prompt.bullet("{} is already this fleet's Edge {}, and it keeps running if you "
                      "leave it out.".format(board["mac"], entry.get("name")))
        return prompt.yes("Use it as the new Edge anyway?", default=False)

    remaining, chosen = list(boards), []
    for index in range(edges):
        question = "Which board is the {}Edge?".format("new " if extending else "") \
            if edges == 1 else "Which board is Edge {} of {}?".format(index + 1, edges)
        fresh = [i for i, b in enumerate(remaining)
                 if not (extending and _registered_as(entries, b["mac"]))]
        # Never the Hub: it is always refused, so Enter would ask the same question forever.
        not_hub = [i for i, b in enumerate(remaining) if not (extending and b["mac"] == hub_mac)]
        default = (fresh or not_hub or [0])[0]
        while True:
            board = prompt.choose(question, [(b, label(b)) for b in remaining],
                                  default=default)
            if confirmed(board):
                break
        remaining.remove(board)
        chosen.append(board)
    return chosen, remaining


def ask_hub(prompt, remaining, entries, extending):
    """Which of the boards left over is the Hub.

    The Hub this fleet already registered is offered first rather than merely allowed, and on
    an extend run any other answer stops the run: extending writes the roster onto the board
    picked here, and a board that is not the deployment's Hub is the wrong place for it. A new
    board replacing a dead Hub has no roster to extend; the `hub` command gives it the full Hub
    phase inside the same fleet, which is what the refusal points to.
    """
    registered = _registered_hub_mac(entries)
    if len(remaining) == 1:
        hub = remaining[0]
        prompt.bullet("the Hub is {}, the board left over.".format(_label(hub)))
    else:
        known_first = sorted(remaining, key=lambda b: b["mac"] != registered)
        hub = prompt.choose(
            "Which board is the Hub?",
            [(b, "{}{}".format(_label(b), "   (the Hub this fleet registered)"
                               if b["mac"] == registered else ""))
             for b in known_first], default=0)
    if extending and registered and hub["mac"] != registered:
        old = _registered_as(entries, registered) or {}
        raise BoardError(
            "{} is not the Hub this fleet registered ({}). Nothing was changed. Plug the "
            "fleet's Hub in and run `{} setup` again. If that Hub is gone and this board "
            "replaces it, `{} hub --fleet <this fleet> --port {} --name {}` sets it up as the "
            "Hub of the same fleet; tools/README.md, \"Replacing a dead Hub\", says which "
            "settings to repeat and what the old Hub took with it.".format(
                hub["mac"], registered, CLI, CLI, hub["port"], old.get("name") or "<its name>"))
    return hub


def ask_names(prompt, chosen, entries, extending):
    """What each Edge in this run is called, which on an extend run is which slot it takes."""
    if extending:
        return ask_edge_names(prompt, chosen, entries)
    return [None] if len(chosen) == 1 else ["S{}".format(i + 1) for i in range(len(chosen))]


def ask_each_board(prompt, chosen, first):
    """What each of these boards is, asked one board at a time.

    Beside the board it is about, for the same reason the name is: what the operator answers is
    "which of these things in front of me is this one". Each answer becomes the next question's
    default, since a mixed pair is the case this exists for and a matched pair is still the
    common one.
    """
    hardware, default = [], first
    for board in chosen:
        default = ask_hardware(prompt, board, default=default)
        hardware.append(default)
    return hardware


def ask_firmware(prompt, images, hardware, extending):
    """Whether to flash, and then which image each board gets. Returns that lookup.

    One question, and then an image per board rather than a path per run. A board is flashed
    with the build for its own target or with nothing: offering one image for the whole run is
    how a mixed pair gets the wrong radio's firmware with every step reporting success.
    """
    # Keeping is the default even with a build on disk. A flash erases the board, its identity
    # with it, so it is something the operator picks rather than something Enter does.
    flashing = prompt.choose(
        "Flash firmware, or keep what the boards run?",
        [(True, "flash the newest build on this machine for each board"),
         (False, "keep what the boards are running  (no flash, nothing erased)")],
        default=1)

    def firmware_for(pair):
        return image_for(target_of(*pair), images) if flashing else None

    if not flashing:
        return firmware_for
    prompt.bullet("a flash erases the board. A secure or control node comes back with a new "
                  "device_id, which the fleet registers, and any old key is kept in the fleet's "
                  "backups folder. Each board takes a couple of minutes.")
    if extending:
        prompt.bullet("only the Edges above are flashed. The Hub is not: extending it changes "
                      "one row in its roster and nothing else.")
    # Said at the moment it becomes true, rather than left to be noticed in the summary. A
    # target with no build on this machine is the ordinary state for the second radio of a
    # mixed pair, and that board is about to be provisioned without being flashed.
    for pair in hardware:
        if firmware_for(pair) is None:
            prompt.bullet("no {} build on this machine, so that board keeps what it runs. Its "
                          "config is still written.".format(target_of(*pair)))
    return firmware_for


def ask_rf(prompt, known, extending):
    """The radio settings both ends of this deployment will share."""
    return {field: prompt.number(
        "Radio {}?".format(spelled),
        default=known["rf"].get(field, DEFAULT_RF[field]) if extending else DEFAULT_RF[field])
        for field, spelled in _ASKED_RF}


def ask_plan(prompt, boards, images, fleet_path, mode="scratch", entries=()):
    """Everything the run needs, settled before anything is touched.

    Posture is asked first because it is the choice the rest of the deployment is built around:
    it decides whether nodes get an identity and whether a control root is minted.
    """
    entries = list(entries)
    known = deployment_defaults(entries)
    extending = mode == "extend"

    posture = ask_posture(prompt, known, entries, extending)
    chosen, remaining = ask_edges(prompt, boards, extending, entries)
    names = ask_names(prompt, chosen, entries, extending)
    edge_hardware = ask_each_board(
        prompt, chosen,
        (known["board"], known["radio"]) if extending else (DEFAULT_BOARD, "sx127x"))

    hub = ask_hub(prompt, remaining, entries, extending)
    # The Hub is not asked what it is when extending: that run changes one row in its roster and
    # never writes its config, so its hardware is a fact the registry already holds and a
    # question here would invite an answer that changes nothing.
    hub_hardware = _registered_hardware(entries, "hub") if extending else \
        ask_hardware(prompt, hub, default=edge_hardware[-1])

    firmware_for = ask_firmware(prompt, images, edge_hardware + [hub_hardware], extending)
    rf = ask_rf(prompt, known, extending)

    return plan_module.build(
        mode=mode, fleet=fleet_path, posture=posture, rf=rf,
        edges=[{"mac": board["mac"], "name": name, "board": hardware[0],
                "radio": hardware[1], "firmware": firmware_for(hardware)}
               for board, name, hardware in zip(chosen, names, edge_hardware)],
        hub={"mac": hub["mac"], "name": None, "board": hub_hardware[0],
             "radio": hub_hardware[1],
             "firmware": None if extending else firmware_for(hub_hardware)})


def _hardware_line(entry):
    """One node's hardware and what will be written to it, in one line under its board.

    The flash half is spelled out per node rather than once per run because it is the half that
    can be wrong invisibly. A board with no build for its target keeps what it runs, and that
    has to be readable in the plan the operator approves rather than discovered afterwards.
    """
    return "{} {}   {}".format(
        entry["board"], entry["radio"],
        "flash " + _readable(entry["firmware"]) if entry["firmware"]
        else "keeps what it runs")


def _readable(path):
    """A firmware path as it is worth reading: relative to the repo, or whole.

    An image kept outside the repository is an ordinary thing to have, and `relpath` renders one
    as a chain of `..` that is longer than the path it replaced and says less.
    """
    relative = os.path.relpath(path, _REPO_ROOT)
    return path if relative.startswith(os.pardir) else relative


def describe_plan(prompt, plan, boards):
    """The plan, in the words of what will happen to which board."""
    extending = plan["mode"] == "extend"
    prompt.say("\n  Plan")
    prompt.bullet("this run  {}".format(
        "extends the deployment above" if extending else "sets up a new deployment"))
    for index, edge in enumerate(plan["edges"], start=1):
        prompt.bullet("Edge {}  {}{}".format(
            index, _label(_find(boards, edge["mac"])),
            "   as {}".format(edge["name"]) if edge["name"] else ""))
        prompt.bullet("        {}".format(_hardware_line(edge)))
    prompt.bullet("Hub     {}{}".format(
        _label(_find(boards, plan["hub"]["mac"])),
        "   roster only, not reflashed" if extending else ""))
    if not extending:
        prompt.bullet("        {}".format(_hardware_line(plan["hub"])))
    prompt.bullet("posture {}{}".format(
        plan["posture"],
        ", session id {}".format(plan["session_id"]) if plan["session_id"] is not None else ""))
    prompt.bullet("radio   sf{}, {} MHz, bw {}, on both ends".format(
        plan["rf"]["sf"], plan["rf"]["freq"], plan["rf"]["bandwidth"]))
    prompt.bullet("fleet   {}".format(plan["fleet"]))
    named = set(plan_module.macs(plan))
    for board in boards:
        if board["mac"] not in named:
            prompt.bullet("left    {}, not part of this run".format(_label(board)))
    prompt.bullet("then    one real {}-byte transfer, Edge 1 to the Hub".format(PAYLOAD_LEN))
    if len(plan["edges"]) > 1:
        prompt.bullet("        the other Edges are provisioned and registered, and the "
                      "transfer proves the first one")
    if extending:
        # The proof drives both boards directly, so the Hub stops serving the deployment for
        # as long as it runs. Worth approving rather than discovering.
        prompt.bullet("        that proof runs on the Hub itself, so this deployment is off "
                      "the air for a couple of minutes at the end")


def _stall_prompt(prompt):
    """What to say when a freshly flashed board has gone quiet.

    One tap and then hands off. Repeated tapping is actively harmful: each one knocks the board
    down again before a probe can reach it, which is how a bench session loses a run.
    """
    def on_stall(port):
        prompt.say("")
        prompt.bullet("the board on {} is still silent, and a reset over the wire did not "
                      "bring it back.".format(port))
        prompt.pause("Tap RESET on that board once, only once, then press Enter.")
    return on_stall


def run_plan(plan, boards, result, prompt=None, runner=None, sleep=None):
    """Do what the plan says, in the order the phases require.

    The one engine behind both front doors: `setup` reaches it with a prompt and a person at
    the keyboard, `apply` reaches it with neither. What a prompt buys is the tap on RESET
    offered inside the run; without one the wait ends in the same refusal the non-interactive
    commands have always given.

    Ports are resolved from MACs here, once, before anything is touched. A plan that names a
    board nobody plugged in fails at the desk rather than after the first board is provisioned
    and the second is still holding whatever it held.
    """
    ports = plan_module.ports_for(plan, boards)
    fleet = Fleet(plan["fleet"])
    extending = plan["mode"] == "extend"
    total = len(plan["edges"]) + 2

    def stage(index, title):
        if prompt:
            prompt.stage(index, total, title)
        else:
            result.step("phase", "{}/{} {}".format(index, total, title))

    on_stall = _stall_prompt(prompt) if prompt else None
    common = dict(posture=plan["posture"], rf=plan["rf"], on_stall=on_stall)

    def hardware(entry):
        """What this one node is made of, as the phases take it."""
        return dict(radio=entry["radio"], board_model=entry["board"],
                    firmware=entry["firmware"])

    # Every board whose phase finished, in order. Started at the end however the run ends: a
    # run that fails on its second Edge has a first Edge that is a finished node, and leaving
    # it at the prompt is a node doing nothing until somebody presses RESET. A board whose own
    # phase failed is not started into whatever half of the files it holds. A board the run
    # never reached was only read, at discovery, which stopped its program all the same, so it
    # is started too: holding exactly the files it ran on, it is safe to.
    finished, reached = [], set()
    try:
        for index, edge in enumerate(plan["edges"], start=1):
            port = ports[edge["mac"]]
            reached.add(edge["mac"])
            stage(index, "Edge {} on {}".format(index, port))
            board = Board(port, runner=runner, sleep=sleep)
            provision_edge(board, fleet, result, name=edge["name"],
                           session_id=plan["session_id"], **dict(common, **hardware(edge)))
            finished.append(board)

        hub_port = ports[plan["hub"]["mac"]]
        reached.add(plan["hub"]["mac"])
        hub_board = Board(hub_port, runner=runner, sleep=sleep)
        if extending:
            # The whole reason extend is its own mode. Adding a node changes one row in one
            # file, and the full Hub phase rewrites four and restarts the board to do it -- over
            # a deployment that is running, and over rows this Hub has settled in the field.
            stage(len(plan["edges"]) + 1, "Hub roster on {}".format(hub_port))
            # The plan's own names, not the ports the Edges came back on: a flashed board can
            # re-enumerate onto a different path, and the name is the slot the registry keys
            # by. An extend plan always names its Edges, which is what `free_edge_names` is for.
            writing = []
            try:
                update_hub_roster(hub_board, fleet, result,
                                  set(edge["name"] for edge in plan["edges"]),
                                  on_write=lambda: writing.append(True))
            except BaseException:
                # Reading the roster stopped the Hub's program, and a field Hub left at the
                # prompt collects nothing until somebody drives out. Before the copy began it
                # holds the files it ran on, so it is restarted; a copy cut short may have left
                # half a Nodes.json, and a Hub started on that fails at boot instead.
                if not writing:
                    finished.append(hub_board)
                raise
        else:
            stage(len(plan["edges"]) + 1, "Hub on {}".format(hub_port))
            provision_hub(hub_board, fleet, result, name=plan["hub"]["name"],
                          session_id=plan["session_id"],
                          **dict(common, **hardware(plan["hub"])))
        finished.append(hub_board)

        stage(total, "Proof: one real transfer")
        # `verify_pair` takes each end's radio from the registry, where this run has just
        # written it: a mixed pair needs two different board-side scripts.
        verify_pair(finished[0], hub_board, fleet, result, posture=plan["posture"])
    finally:
        start_programs(finished + [Board(port, runner=runner, sleep=sleep)
                                   for mac, port in ports.items() if mac not in reached],
                       result)
    return fleet


def _machine_is_ready(prompt, result, runner=None):
    """`doctor`, asked as a question rather than reported as a table.

    A phase that dies halfway through for a missing tool leaves a half-configured board, so
    this is the one check that has to happen before anything else, every time.
    """
    report = doctor_module.check(runner=runner)
    missing = doctor_module.missing(report)
    if not missing:
        prompt.bullet("this machine can drive a board: " + ", ".join(
            entry["tool"] for entry in report))
        return True
    prompt.bullet("missing: " + ", ".join(entry["tool"] for entry in missing))
    for entry in missing:
        prompt.bullet("  {} is needed for {}".format(entry["tool"], entry["why"]))
        prompt.bullet("  install with: " + entry["plan"]["command"])
        if entry["plan"]["scripts_dir"] and not entry["plan"]["on_path"]:
            # The install would succeed and the wizard still would not find it, which reads as
            # the install having failed. Said before it runs, not after it disappoints.
            prompt.bullet("  warning: that lands in {}, which is not on PATH. Add it, or "
                          "install from an interpreter whose scripts directory already "
                          "is.".format(entry["plan"]["scripts_dir"]))
    if not prompt.yes("Install {} now?".format(
            " and ".join(entry["tool"] for entry in missing))):
        raise BoardError(
            "{} missing. `{} doctor --install` does this on its own, or install it "
            "by hand and run `setup` again.".format(
                ", ".join(entry["tool"] for entry in missing), CLI))
    for entry in missing:
        requirement = next(r for r in doctor_module.REQUIREMENTS if r.key == entry["tool"])
        outcome = doctor_module.install(requirement, runner=runner)
        result.step("install " + entry["tool"], outcome["detail"],
                    status=OK if outcome["ok"] else SKIPPED)
    still = doctor_module.missing(doctor_module.check(runner=runner))
    if still:
        raise BoardError("still missing after the install: " + ", ".join(
            entry["tool"] for entry in still))
    return True


def _next_steps(prompt, plan, entries, boards):
    """What to do with a pair that is now running, in commands that work as printed.

    The Hub files what arrives under the Edge's off-air label, never under its name, so the
    folder is worked out by the library's own rule rather than written here a second time.
    Copying a file stops the Edge's program, so the copy ends with the reset that restarts it.
    """
    # The newest record: a reflashed board keeps its MAC, and its old record stays ahead.
    edge = next((e for e in reversed(entries)
                 if e.get("role") == "edge" and e.get("mac") == plan["edges"][0]["mac"]), None)
    if edge is None:
        return
    label = label_for_config({"device_id": edge.get("device_id"), "mac_address": edge["mac"]})
    edge_port = _find(boards, edge["mac"])["port"]
    hub_port = _find(boards, plan["hub"]["mac"])["port"]
    prompt.say("\n  Next")
    prompt.bullet("to send a file, put it in the {}Edge's {}:".format(
        "first " if len(plan["edges"]) > 1 else "", DEFAULT_QUEUE_PATH))
    prompt.bullet("  mpremote connect {} fs cp my-file.txt :{}/my-file.txt + reset".format(
        edge_port, DEFAULT_QUEUE_PATH))
    prompt.bullet("the Hub saves it under {}/{}/, the label it gives this Edge.".format(
        DEFAULT_RESULT_PATH, label))
    prompt.bullet("to watch the Hub work (Ctrl-] leaves):")
    prompt.bullet("  mpremote connect {} repl".format(hub_port))
    prompt.bullet("if a port is gone, `{} ports` lists each board by MAC.".format(CLI))


def _summarise(prompt, plan, fleet, result, plan_path, boards):
    prompt.say("\n  Done. {}".format(fleet.path))
    entries = fleet.entries()
    for entry in entries:
        prompt.bullet("{:<6} {:<5} {:<8} {}".format(
            entry.get("name", "?"), entry.get("role", "?"), entry.get("posture", "?"),
            entry.get("device_id", "") or "(no identity)"))
    if any(e.get("name") == DEFAULT_NAMES.get(e.get("role")) for e in entries):
        prompt.bullet("{} and {} are the names an Edge and a Hub get when nobody chose one. "
                      "They are for people: nothing is addressed by them.".format(
                          DEFAULT_NAMES["edge"], DEFAULT_NAMES["hub"]))
    if plan["posture"] == "control":
        prompt.bullet("the signing half of this deployment's root is in {}. It is the "
                      "authority: keep it off shared drives and out of git.".format(
                          fleet.root_key_path))
    prompt.bullet("that directory is the record of this deployment.")
    prompt.bullet("`{0} setup` again adds nodes to it, `{0} fleet-show` reads it back.".format(CLI))
    prompt.bullet("what this run did is written down in {}. `{} apply` runs it again, on the "
                  "same boards, without the questions.".format(plan_path, CLI))
    _next_steps(prompt, plan, entries, boards)


def _ask_fresh_fleet(prompt, taken):
    """Where a new deployment goes when the directory named already holds one.

    Never over the old one. A fleet directory is a control root, its mint counter and the
    record of every node pinned to it; starting a second deployment on top of the first would
    leave the running nodes' authority in a directory that now describes somebody else.
    """
    prompt.bullet("a new deployment gets its own folder. This one holds the keys and records "
                  "of the nodes above, so it is left as it is.")
    while True:
        path = prompt.text("Folder name for the new deployment",
                           default=taken.rstrip("/") + "-2")
        if Fleet(path).entries():
            prompt.bullet("{} already holds a deployment too. Name one that does not, or run "
                          "`{} setup` again and extend one of them.".format(path, CLI))
            continue
        return path


def _with_a_mac(prompt, boards):
    """The boards that can be named in a plan, refusing up front when that leaves too few.

    A plan names a board by its MAC, so a board whose MAC could not be read cannot be in one.
    Found after "Proceed?", it is a run that fails with every answer already given.
    """
    named = [board for board in boards if board["mac"]]
    unread = [board["port"] for board in boards if not board["mac"]]
    if not unread:
        return named
    if len(named) < 2:
        raise BoardError(
            "could not read the MAC of the board on {}, and a board is named by its MAC. "
            "Unplug it and plug it back in, then run this again. If it still has no MAC, it is "
            "not running MicroPython with WiFi support, which these boards need.".format(
                ", ".join(unread)))
    prompt.bullet("left out: {}. Its MAC could not be read, so it cannot be named in the "
                  "plan.".format(", ".join(unread)))
    return named


def run(args, result, runner=None, sleep=None, ask=None, out=None):
    """The whole command: check, discover, ask, confirm, write the plan, run it, summarise."""
    prompt = Prompt(read=ask, out=out)
    prompt.say("\n  AlLoRa setup")
    prompt.bullet("this asks what you want, shows you the plan, and then runs it.")

    prompt.say("\n  Checking this machine")
    _machine_is_ready(prompt, result, runner=runner)

    # Before the first question rather than at the first key written: refused after "Proceed?",
    # the operator loses every answer they gave to get there.
    Fleet(args.fleet).refuse_if_git_would_commit_keys()

    prompt.say("\n  Looking for boards")
    found = look_for_boards(runner=runner)
    owned = set()
    try:
        return _converse_and_run(prompt, args, result, found, owned, runner, sleep)
    finally:
        restart_the_rest(found, owned, result, runner=runner)


def _converse_and_run(prompt, args, result, found, owned, runner, sleep):
    """The questions, the plan and the run. `owned` is filled with the MACs the run takes over
    the moment it starts, so the caller restarts every other board it looked at."""
    for board in found:
        prompt.bullet(_label(board))
    boards = _with_a_mac(prompt, found)
    if len(boards) < 2:
        # Both ends are provisioned in one run because the Hub is registered against the
        # Edge's fingerprint: one board is half a deployment, and the half it is missing is
        # the half that would have to be typed in by hand later.
        raise BoardError(
            "found {} board(s) answering a MicroPython REPL, and a deployment needs two: an "
            "Edge and a Hub. Plug both in and run this again. Other USB serial devices show "
            "up under the same names, so only a port that answered is counted.".format(
                len(boards)))

    fleet_path = args.fleet
    entries = Fleet(fleet_path).entries()
    mode = ask_mode(prompt, entries)
    if mode == "scratch" and entries:
        fleet_path = _ask_fresh_fleet(prompt, fleet_path)
        # Asked of the folder the run will actually write, for the same reason as above.
        Fleet(fleet_path).refuse_if_git_would_commit_keys()
        entries = []
    hub_mac = _registered_hub_mac(entries)
    if mode == "extend" and hub_mac and all(b["mac"] != hub_mac for b in boards):
        # Extending writes onto this board, so without it the run can only fail, and asked at
        # the Hub question it fails after every Edge answer has been given.
        raise BoardError(
            "this fleet's Hub ({}) is not plugged in, and extending writes the new nodes onto "
            "it. Nothing was changed. Plug it in and run `{} setup` again; to replace a Hub "
            "that is gone, see tools/README.md, \"Replacing a dead Hub\".".format(hub_mac, CLI))

    plan = ask_plan(prompt, boards, firmware_images(), fleet_path, mode=mode, entries=entries)
    describe_plan(prompt, plan, boards)
    if not prompt.yes("Proceed?"):
        raise BoardError("nothing was changed. Run `{} setup` again when you are "
                         "ready.".format(CLI))

    # Written before the first board is touched, because it is the record of what was
    # approved. A run that dies halfway leaves the plan behind, and `provision apply` picks it
    # up rather than making the operator answer every question again to get back to here.
    fleet = Fleet(fleet_path)
    fleet.ensure()
    plan_path = plan_module.write(fleet.plan_path, plan)
    prompt.bullet("plan written to {}".format(plan_path))

    owned.update(plan_module.macs(plan))
    try:
        fleet = run_plan(plan, boards, result, prompt, runner=runner, sleep=sleep)
    except BoardError:
        # Whatever got as far as being registered stays registered, and re-running picks up
        # from there rather than starting the deployment again. Said here because the phase
        # that failed knows why it failed and not what surrounds it.
        prompt.say("")
        prompt.bullet("the nodes that were finished are recorded in {}. Fix what the step "
                      "above says, then run `{} apply {}`: it adds to that fleet "
                      "rather than starting a new one, and asks nothing.".format(
                          fleet_path, CLI, plan_path))
        raise
    result.set(plan=plan, plan_path=plan_path, fleet=fleet.path)
    if result.ok:
        _summarise(prompt, plan, fleet, result, plan_path, boards)
    return result


def apply(doc, result, runner=None, sleep=None):
    """The second front door: run a plan somebody else wrote, and ask nothing.

    Same engine as `setup`, reached without a keyboard. This is what lets something other than
    a person at a terminal drive a provisioning run: a website writes the plan, this runs it,
    and `--json` carries back what happened. The plan is already read and validated by the
    time it arrives, because the caller has to know whether it names a firmware before it can
    check this machine for the tool that flashes one.
    """
    result.step("plan", "{} mode, {} edge(s), fleet {}".format(
        doc["mode"], len(doc["edges"]), doc["fleet"]))
    boards = look_for_boards(runner=runner)
    owned = set()
    try:
        # Refused here when a named board is missing, before any is touched, so every board
        # read so far is still only looked at and gets its restart.
        plan_module.ports_for(doc, boards)
        owned.update(plan_module.macs(doc))
        fleet = run_plan(doc, boards, result, prompt=None, runner=runner, sleep=sleep)
    finally:
        restart_the_rest(boards, owned, result, runner=runner)
    result.set(plan=doc, fleet=fleet.path)
    return result

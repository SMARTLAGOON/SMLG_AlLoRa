"""`provision setup`: the one command, and the only interactive one.

The rest of this package is a toolkit -- five commands that each do one phase correctly and
never ask a question. That shape is right for the control website, which shells out and parses
`--json`, and wrong for a person at a bench: it makes them know the five commands exist, know
the order, know which posture they want, and hand-type port paths for two boards that are told
apart by MAC rather than by path.

`setup` is the layer on top and nothing else. It **drives** `provision_edge`, `provision_hub`
and `verify_pair`; it does not reimplement them. Everything it decides is decided before any
board is touched, and then it runs the plan the operator approved.

Three things make it more than a shell loop over the other commands:

**It discovers the boards and lets you pick which is which.** The MAC is the identity; the port
is not, and on native USB the port changes under you on every hard reset.

**It holds a plan you approve once**, and then runs unattended.

**It owns the human-in-the-loop moment.** When a freshly flashed board stays silent, the tap on
RESET is a prompt *inside* the run rather than a failure, a message and a re-run from the top.

Every question goes through `Prompt`, whose reader is injected, so the whole flow is testable
with a scripted list of answers and no keyboard.
"""
import os
import sys

from tools.allora_provision import doctor as doctor_module
from tools.allora_provision.board import Board, BoardError, discover_boards
from tools.allora_provision.fleet import Fleet
from tools.allora_provision.node_config import DEFAULT_RF, POSTURES
from tools.allora_provision.result import OK, SKIPPED
from tools.allora_provision.steps import (PAYLOAD_LEN, RADIOS, provision_edge, provision_hub,
                                           verify_pair)

# Where a build lands. Searched rather than named so the operator picks a firmware off a list
# instead of remembering a path that changes with every CI run.
FIRMWARE_DIR = "firmware"
_MAX_IMAGES = 6

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# What each posture means, in the words somebody choosing between them needs. The full
# consequences of `control` are printed by the phase that provisions it; this is the sentence
# that gets the choice right.
_POSTURE_HELP = {
    "open": "no identity, no encryption. Both ends share a number you assign.",
    "secure": "each node generates an identity and proves it. Frames are sealed.",
    "control": "secure, plus a pinned root: the node then takes only signed commands.",
}

_RADIO_HELP = {
    "sx127x": "T3S3 / most SX1276 boards",
    "sx1262": "Heltec V3, T3S3 SX1262",
    "e5": "Wio-E5",
    "lopy4": "Pycom LoPy4",
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
        suffix = " [{}]: ".format(default) if default is not None else ": "
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


def firmware_images(root=None, limit=_MAX_IMAGES):
    """The firmware builds on this machine, newest first.

    Newest first because the answer is nearly always the build that was just downloaded, and a
    list ordered by path would bury it under the ones that came before.
    """
    base = os.path.join(root or _REPO_ROOT, FIRMWARE_DIR)
    found = []
    for directory, _, names in os.walk(base):
        for name in names:
            if name.endswith(".bin"):
                path = os.path.join(directory, name)
                found.append((os.path.getmtime(path), path))
    found.sort(reverse=True)
    return [path for _, path in found[:limit]]


def look_for_boards(runner=None):
    """Every board answering right now, with the MAC that says which one it is."""
    boards = []
    for port in discover_boards(runner=runner):
        boards.append({"port": port, "mac": Board(port, runner=runner).mac()})
    return boards


def _label(board):
    return "{}  on {}".format(board["mac"] or "MAC unknown", board["port"])


def ask_plan(prompt, boards, images, fleet_path):
    """Everything the run needs, settled before anything is touched.

    Posture is asked first because it constrains the rest: it decides whether a session id is
    needed, whether a control root is minted, and -- since open addressing is one number handed
    to one pair -- how many Edges this command is willing to provision in a run.
    """
    posture = prompt.choose(
        "Security posture?",
        [(name, "{:<8} {}".format(name, _POSTURE_HELP[name])) for name in POSTURES],
        default=list(POSTURES).index("secure"))

    most = len(boards) - 1
    if posture == "open":
        # An open pair is addressed by a number both ends carry, assigned by hand. Handing out
        # several of those is a scheme, and the wizard does not invent one: it does the pair.
        if most > 1:
            prompt.bullet("open addressing is one number shared by one pair, so this run "
                          "provisions a single Edge. `provision edge --session-id` takes the "
                          "others, or use the secure posture, where the address derives from "
                          "the identity.")
        most = 1
    edges = 1 if most <= 1 else prompt.number(
        "How many Edge nodes in this deployment?", default=1, low=1, high=most)

    remaining = list(boards)
    chosen = []
    for index in range(edges):
        question = "Which board is the Edge?" if edges == 1 else \
            "Which board is Edge {} of {}?".format(index + 1, edges)
        board = prompt.choose(question, [(b, _label(b)) for b in remaining], default=0)
        remaining.remove(board)
        chosen.append(board)
    hub = prompt.choose("Which board is the Hub?", [(b, _label(b)) for b in remaining],
                        default=0) if len(remaining) > 1 else remaining[0]
    if len(remaining) == 1:
        prompt.bullet("the Hub is {}, the board left over.".format(_label(hub)))

    session_id = None
    if posture == "open":
        session_id = prompt.number(
            "Which session id should the pair share?", default=1, low=0, high=255)

    keep = (None, "keep what the boards are running  (no flash, nothing erased)")
    firmware = prompt.choose(
        "Flash firmware, or keep what the boards run?",
        [(path, os.path.relpath(path, _REPO_ROOT)) for path in images] + [keep],
        default=len(images))
    if firmware:
        prompt.bullet("a flash erases the board: its identity is backed up into the fleet "
                      "first, and each board takes a couple of minutes.")

    radio = prompt.choose("Which radio do these boards have?",
                          [(name, "{:<7} {}".format(name, _RADIO_HELP[name]))
                           for name in sorted(RADIOS)],
                          default=sorted(RADIOS).index("sx127x"))

    rf = {}
    for field, spelled in _ASKED_RF:
        rf[field] = prompt.number("Radio {}?".format(spelled), default=DEFAULT_RF[field])

    names = [None] if edges == 1 else ["S{}".format(i + 1) for i in range(edges)]
    return {"posture": posture, "session_id": session_id, "firmware": firmware,
            "radio": radio, "rf": rf, "fleet": fleet_path,
            "edges": [dict(board, name=name) for board, name in zip(chosen, names)],
            "hub": dict(hub, name=None),
            "untouched": [b for b in remaining if b is not hub]}


def describe_plan(prompt, plan):
    """The plan, in the words of what will happen to which board."""
    prompt.say("\n  Plan")
    for index, edge in enumerate(plan["edges"], start=1):
        prompt.bullet("Edge {}  {}".format(index, _label(edge)))
    prompt.bullet("Hub     {}".format(_label(plan["hub"])))
    prompt.bullet("posture {}{}".format(
        plan["posture"],
        ", session id {}".format(plan["session_id"]) if plan["session_id"] is not None else ""))
    prompt.bullet("radio   {} at sf{}, {} MHz, bw {}".format(
        plan["radio"], plan["rf"]["sf"], plan["rf"]["freq"], plan["rf"]["bandwidth"]))
    prompt.bullet("flash   {}".format(
        os.path.relpath(plan["firmware"], _REPO_ROOT) if plan["firmware"]
        else "no; the boards keep what they run"))
    prompt.bullet("fleet   {}".format(plan["fleet"]))
    for board in plan.get("untouched", []):
        prompt.bullet("left    {}, not part of this deployment".format(_label(board)))
    prompt.bullet("then    one real {}-byte transfer, Edge 1 to the Hub".format(PAYLOAD_LEN))
    if len(plan["edges"]) > 1:
        prompt.bullet("        the other Edges are provisioned and registered, and the "
                      "transfer proves the first one")


def _stall_prompt(prompt):
    """What to say when a freshly flashed board has gone quiet.

    One tap and then hands off. Repeated tapping is actively harmful: each one knocks the board
    down again before a probe can reach it, which is how a bench session loses a run.
    """
    def on_stall(port):
        prompt.say("")
        prompt.bullet("the board on {} is still silent, and a reset over the wire did not "
                      "bring it back.".format(port))
        prompt.pause("Tap RESET once on that board -- once only -- then press Enter.")
    return on_stall


def run_plan(plan, result, prompt, runner=None, sleep=None):
    """Do what was approved, in the order the phases require."""
    fleet = Fleet(plan["fleet"])
    total = len(plan["edges"]) + 2
    on_stall = _stall_prompt(prompt)
    common = dict(posture=plan["posture"], firmware=plan["firmware"], rf=plan["rf"],
                  radio=plan["radio"], on_stall=on_stall)

    provisioned = []
    for index, edge in enumerate(plan["edges"], start=1):
        prompt.stage(index, total, "Edge {} on {}".format(index, edge["port"]))
        board = Board(edge["port"], runner=runner, sleep=sleep)
        provision_edge(board, fleet, result, name=edge["name"],
                       session_id=plan["session_id"], **common)
        provisioned.append(board)

    prompt.stage(len(plan["edges"]) + 1, total, "Hub on {}".format(plan["hub"]["port"]))
    hub_board = Board(plan["hub"]["port"], runner=runner, sleep=sleep)
    provision_hub(hub_board, fleet, result, name=plan["hub"]["name"],
                  session_id=plan["session_id"], **common)

    prompt.stage(total, total, "Proof: one real transfer")
    verify_pair(provisioned[0], hub_board, fleet, result,
                posture=plan["posture"], radio=plan["radio"])
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
            "{} missing. `provision doctor --install` does this on its own, or install it "
            "by hand and run `setup` again.".format(
                ", ".join(entry["tool"] for entry in missing)))
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


def _summarise(prompt, plan, fleet, result):
    prompt.say("\n  Done. {}".format(fleet.path))
    for entry in fleet.entries():
        prompt.bullet("{:<6} {:<5} {:<8} {}".format(
            entry.get("name", "?"), entry.get("role", "?"), entry.get("posture", "?"),
            entry.get("device_id", "") or "(no identity)"))
    if plan["posture"] == "control":
        prompt.bullet("the signing half of this deployment's root is in {}. It is the "
                      "authority: keep it off shared drives and out of git.".format(
                          fleet.root_key_path))
    prompt.bullet("that directory is the record of this deployment.")
    prompt.bullet("`provision setup` again adds nodes to it, `provision fleet-show` reads it "
                  "back.")


def run(args, result, runner=None, sleep=None, ask=None, out=None):
    """The whole command: check, discover, ask, confirm, run, summarise."""
    prompt = Prompt(read=ask, out=out)
    prompt.say("\n  AlLoRa setup")
    prompt.bullet("this asks what you want, shows you the plan, and then runs it.")

    prompt.say("\n  Checking this machine")
    _machine_is_ready(prompt, result, runner=runner)

    prompt.say("\n  Looking for boards")
    boards = look_for_boards(runner=runner)
    for board in boards:
        prompt.bullet(_label(board))
    if len(boards) < 2:
        # Both ends are provisioned in one run because the Hub is registered against the
        # Edge's fingerprint: one board is half a deployment, and the half it is missing is
        # the half that would have to be typed in by hand later.
        raise BoardError(
            "found {} board(s) answering a MicroPython REPL, and a deployment needs two: an "
            "Edge and a Hub. Plug both in and run this again. Other USB serial devices show "
            "up under the same names, so only a port that answered is counted.".format(
                len(boards)))

    plan = ask_plan(prompt, boards, firmware_images(), args.fleet)
    describe_plan(prompt, plan)
    if not prompt.yes("Proceed?"):
        raise BoardError("nothing was changed. Run `provision setup` again when you are ready.")

    try:
        fleet = run_plan(plan, result, prompt, runner=runner, sleep=sleep)
    except BoardError:
        # Whatever got as far as being registered stays registered, and re-running picks up
        # from there rather than starting the deployment again. Said here because the phase
        # that failed knows why it failed and not what surrounds it.
        prompt.say("")
        prompt.bullet("the nodes that were finished are recorded in {}. Fix what the step "
                      "above says, then run `provision setup` again: it adds to that fleet "
                      "rather than starting a new one.".format(args.fleet))
        raise
    result.set(plan={k: v for k, v in plan.items() if k not in ("edges", "hub")},
               fleet=fleet.path)
    if result.ok:
        _summarise(prompt, plan, fleet, result)
    return result

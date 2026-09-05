"""The command line: one command per board, and `--json` on every one of them.

Machine-readable output is the integration surface. The website's backend runs the same code
path a human does, and `AlLoRaControl` is TypeScript, so it shells out to this rather than
importing anything: what makes that work is that stdout under `--json` is one parseable
document and nothing else.

The gesture the whole tool is judged on is **one value per node, copied once**. The Hub needs
the Edge's `device_id`; the Edge needs nothing about the Hub. So `edge` prints and records a
fingerprint, `hub` reads it back out of the fleet, and at no point does an operator hold two
values for one node.
"""
import argparse
import sys

from tools.allora_provision import doctor as doctor_module
from tools.allora_provision import plan as plan_module
from tools.allora_provision import setup as setup_module
from tools.allora_provision.board import Board, BoardError, Runner, discover_boards
from tools.allora_provision.fleet import Fleet
from tools.allora_provision.node_config import BOARDS, DEFAULT_BOARD, DEFAULT_RF, POSTURES
from tools.allora_provision.result import FAILED, OK, Result, SKIPPED
from tools.allora_provision.steps import (
    RADIOS, provision_edge, provision_hub, verify_pair)

DEFAULT_FLEET = "allora-fleet"

# A visible directory next to the operator's work, not a hidden one under the home directory.
# The private root and its counter have to be handed over as files when a deployment moves from
# the bench to a backend, and a path somebody has to be told about is a path they will lose.
_FLEET_HELP = ("where this deployment's control root, mint counter and node registry live "
               "(default: ./{})".format(DEFAULT_FLEET))


def _rf_from(args):
    return {key: getattr(args, key.replace("-", "_"), None) for key in DEFAULT_RF}


def _add_common(parser):
    parser.add_argument("--fleet", default=DEFAULT_FLEET, help=_FLEET_HELP)
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help="emit one JSON document on stdout; progress goes to stderr")


def _add_radio(parser):
    parser.add_argument("--radio", default="sx127x", choices=sorted(RADIOS),
                        help="which connector the board's program uses (default: sx127x)")
    for field in ("sf", "freq", "bandwidth", "coding_rate", "tx_power"):
        parser.add_argument("--" + field.replace("_", "-"), type=int, default=None,
                            help="radio {} (default: {})".format(field, DEFAULT_RF[field]))
    parser.add_argument("--min-timeout", type=float, default=None, dest="min_timeout",
                        help=argparse.SUPPRESS)
    parser.add_argument("--max-timeout", type=float, default=None, dest="max_timeout",
                        help=argparse.SUPPRESS)
    parser.add_argument("--timeout-delta", type=float, default=None, dest="timeout_delta",
                        help=argparse.SUPPRESS)


def _add_node(parser):
    _add_common(parser)
    _add_radio(parser)
    parser.add_argument("--port", help="the serial port the board answers on; discovered if "
                                       "exactly one board is present")
    parser.add_argument("--board", default=DEFAULT_BOARD, choices=sorted(BOARDS),
                        dest="board_model",
                        help="what the radio is soldered to, which is what says where it is "
                             "wired (default: {})".format(DEFAULT_BOARD))
    parser.add_argument("--posture", default="secure", choices=list(POSTURES),
                        help="open, secure, or control (default: secure)")
    parser.add_argument("--firmware", help="a firmware .bin to flash first; omitted, the board "
                                           "keeps whatever it is running")
    parser.add_argument("--name", help="what to call this node")
    parser.add_argument("--session-id", type=int, dest="session_id",
                        help="the 1-byte address an OPEN pair shares; secure pairs derive it")
    parser.add_argument("--allow-identity-loss", action="store_true", dest="allow_identity_loss",
                        help="flash even though this board's identity could not be saved")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="provision",
        description="Flash, provision and verify an AlLoRa deployment.")
    subparsers = parser.add_subparsers(dest="command")

    setup = subparsers.add_parser(
        "setup", help="the guided path: asks what you want, shows the plan, runs it")
    setup.add_argument("--fleet", default=DEFAULT_FLEET, help=_FLEET_HELP)
    # No `--json` on this one. The others emit a document for the website to parse; this one
    # holds a conversation, and a machine has nothing to say to it.
    setup.set_defaults(json_mode=False)

    apply = subparsers.add_parser(
        "apply", help="run a plan `setup` wrote, or a website did: same engine, no questions")
    apply.add_argument("plan", help="the plan file to run")
    # `--fleet` is a genuine override here rather than a default, because the plan already
    # carries the deployment it belongs to. Whether it was given is the thing that matters, so
    # the default is None and the plan's own value stands when nobody said otherwise.
    apply.add_argument("--fleet", default=None,
                       help="run this plan against a different deployment than the one it "
                            "names (default: the plan's own)")
    apply.add_argument("--json", action="store_true", dest="json_mode",
                       help="emit one JSON document on stdout; progress goes to stderr")

    ports = subparsers.add_parser(
        "ports", help="list the serial ports that answer a MicroPython REPL")
    _add_common(ports)

    fleet_init = subparsers.add_parser(
        "fleet-init", help="mint this deployment's control root, or report the one it has")
    _add_common(fleet_init)

    fleet_show = subparsers.add_parser(
        "fleet-show", help="what this fleet holds: its root, its counter, its nodes")
    _add_common(fleet_show)

    edge = subparsers.add_parser("edge", help="provision the Edge (the node that has the data)")
    _add_node(edge)

    hub = subparsers.add_parser("hub", help="provision the Hub (the node that drives the pull)")
    _add_node(hub)
    hub.add_argument("--on-site-root", action="store_true", dest="on_site_root",
                     help="put the SIGNING half on this Hub, making it the authority rather "
                          "than a courier. Supported, named, and not the default.")

    doctor = subparsers.add_parser(
        "doctor", help="check this machine can drive a board, and fix what it cannot")
    _add_common(doctor)
    doctor.add_argument("--install", action="store_true",
                        help="install whatever is missing, with pip, into the interpreter "
                             "running this wizard")
    doctor.add_argument("--python", default=None,
                        help="install into this interpreter instead (default: the one running "
                             "this wizard)")

    verify = subparsers.add_parser(
        "verify", help="drive one real transfer and check what actually happened")
    _add_common(verify)
    _add_radio(verify)
    verify.add_argument("--edge-port", dest="edge_port", required=True)
    verify.add_argument("--hub-port", dest="hub_port", required=True)
    verify.add_argument("--posture", default="secure", choices=list(POSTURES))
    verify.add_argument("--window", type=int, default=120,
                        help="seconds each side is given (default: 120)")
    return parser


def _resolve_port(given, result, runner=None):
    if given:
        return given
    answering = discover_boards(runner=runner)
    if len(answering) == 1:
        result.step("discovery", "one board answering, on " + answering[0])
        return answering[0]
    if not answering:
        raise BoardError(
            "no board answers a MicroPython REPL. Plug one in and check it appears: other USB "
            "serial devices show up under the same names, so the wizard only ever adopts a "
            "port that answered.")
    raise BoardError(
        "{} boards are answering ({}). Pass --port to say which one this is: plugging them in "
        "one at a time is the only way to know which physical board is which.".format(
            len(answering), ", ".join(answering)))


def cmd_ports(args, result, runner=None, sleep=None, **_):
    answering = discover_boards(runner=runner)
    boards = []
    for port in answering:
        mac = Board(port, runner=runner).mac()
        boards.append({"port": port, "mac": mac})
        result.note("  {}  MAC {}".format(port, mac or "unknown"))
    if not answering:
        result.note("  (none; other USB serial devices do not count and are not listed)")
    else:
        result.note("\nTwo identical boards are told apart by the MAC, not by the port: the "
                    "port changes on every hard reset because native USB re-enumerates.")
    result.set(ports=answering, boards=boards)
    return result


def cmd_fleet_init(args, result, **_):
    fleet = Fleet(args.fleet)
    root, created = fleet.load_or_create_root()
    result.step("fleet", fleet.path)
    result.step("control root",
                "minted" if created else "already here, keeping it")
    if not created:
        result.note("A root is never replaced: every node is pinned to the one it was given, "
                    "so a fresh one would lock this fleet out of its own control plane.")
    result.step("fingerprint", root.fingerprint().hex())
    result.set(fleet=fleet.path, created=created,
               fingerprint=root.fingerprint().hex(),
               public_key=root.public_key_hex(), counter=fleet.counter())
    result.note("The signing half is at {}. It is this deployment's authority: keep it off "
                "shared drives and out of git, and if it moves to a backend, move {} with it "
                "or the new signer starts numbering at zero and every artifact it mints is "
                "refused as a replay.".format(fleet.root_key_path, fleet.counter_path))
    return result


def cmd_fleet_show(args, result, **_):
    fleet = Fleet(args.fleet)
    root = fleet.root()
    result.step("fleet", fleet.path)
    if root is None:
        result.step("control root", "none minted", status=SKIPPED)
    else:
        result.step("control root", root.fingerprint().hex())
        result.step("counter", "{} minted".format(fleet.counter()))
    for entry in fleet.entries():
        result.note("  {:<10} {:<5} {:<8} {}".format(
            entry.get("name", "?"), entry.get("role", "?"), entry.get("posture", "?"),
            entry.get("device_id", "") or "(no identity)"))
    result.set(fleet=fleet.path, nodes=fleet.entries(),
               fingerprint=root.fingerprint().hex() if root else None,
               counter=fleet.counter())
    return result


def cmd_edge(args, result, runner=None, sleep=None, **_):
    fleet = Fleet(args.fleet)
    preflight(args, result, runner=runner)
    port = _resolve_port(args.port, result, runner=runner)
    board = Board(port, runner=runner, sleep=sleep)
    provision_edge(board, fleet, result, posture=args.posture, firmware=args.firmware,
                   name=args.name, rf=_rf_from(args), session_id=args.session_id,
                   radio=args.radio, allow_identity_loss=args.allow_identity_loss,
                   board_model=args.board_model)
    if result.data.get("device_id"):
        result.note("\nThe Hub needs this one value and nothing else:\n  {}\nIt is already in "
                    "{}, so `provision hub` will pick it up without you copying "
                    "it.".format(result.data["device_id"], fleet.registry_path))
    if args.posture != "control":
        # Offered and explained, never default. A node holding a control root refuses unsigned
        # in-band commands from then on, and that is a real change to what the deployment
        # accepts: it is the operator's call to make knowingly, not one to inherit.
        result.note(
            "\nThis node takes configuration commands over the link itself, unsigned. That is "
            "fine for a survey run, where the link authenticates nothing anyway.\n"
            "The alternative is `--posture control`. You mint a control root once, this node "
            "pins the verifying half, and from then on it takes only commands signed with that "
            "root and refuses unsigned ones. The signing half stays with you, so a stolen Hub "
            "can relay a command but cannot create one.\n"
            "The cost is that the refusal is permanent for this node until it is "
            "re-provisioned, and that you now have a key to keep.")
    return result


def cmd_hub(args, result, runner=None, sleep=None, **_):
    fleet = Fleet(args.fleet)
    preflight(args, result, runner=runner)
    port = _resolve_port(args.port, result, runner=runner)
    board = Board(port, runner=runner, sleep=sleep)
    provision_hub(board, fleet, result, posture=args.posture, firmware=args.firmware,
                  name=args.name, rf=_rf_from(args), session_id=args.session_id,
                  radio=args.radio, on_site_root=args.on_site_root,
                  allow_identity_loss=args.allow_identity_loss,
                  board_model=args.board_model)
    return result


def cmd_verify(args, result, runner=None, sleep=None, **_):
    fleet = Fleet(args.fleet)
    preflight(args, result, runner=runner)
    verify_pair(Board(args.edge_port, runner=runner, sleep=sleep),
                Board(args.hub_port, runner=runner, sleep=sleep),
                fleet, result, posture=args.posture, radio=args.radio, window=args.window)
    return result


def _describe_plan(plan, result):
    result.note("      install with: " + plan["command"])
    if not plan["scripts_dir"]:
        # Only reachable for a --python we could not question. The command is still the right
        # command; what cannot be promised is where its executable will land.
        result.note("      lands in:     could not work it out for that interpreter")
        return
    result.note("      lands in:     " + plan["scripts_dir"])
    if not plan["on_path"]:
        result.warn("{} would be installed to {}, which is not on PATH. The install would "
                    "succeed and the wizard still would not find it. Add the directory to PATH "
                    "(export PATH=\"{}:$PATH\"), or install from an interpreter whose scripts "
                    "directory is already on it.".format(
                        plan["package"], plan["scripts_dir"], plan["scripts_dir"]))


def cmd_doctor(args, result, runner=None, sleep=None, **_):
    runner = runner or Runner()
    result.step("python", "{} at {}".format(
        ".".join(str(n) for n in sys.version_info[:3]), sys.executable))

    report = doctor_module.check(runner=runner, python=args.python)
    for entry in report:
        if entry["found"]:
            result.step(entry["tool"], "{} ({})".format(entry["version"] or "found",
                                                        entry["path"]))
            if len(entry["copies"]) > 1:
                result.warn("{} has {} copies on PATH and the first one wins: {}. Two "
                            "interpreters have been pip-installed into, so the build the "
                            "wizard runs is not necessarily the one a later `pip install` "
                            "updates.".format(entry["tool"], len(entry["copies"]),
                                              ", ".join(entry["copies"])))
        else:
            result.step(entry["tool"], "not on PATH", status=SKIPPED)
            result.note("      needed for:   " + entry["why"])
            _describe_plan(entry["plan"], result)

    outstanding = doctor_module.missing(report)
    installed = []
    if outstanding and args.install:
        for entry in outstanding:
            requirement = next(r for r in doctor_module.REQUIREMENTS
                               if r.key == entry["tool"])
            outcome = doctor_module.install(requirement, python=args.python, runner=runner)
            installed.append({"tool": entry["tool"], "ok": outcome["ok"],
                              "detail": outcome["detail"]})
            result.step("install " + entry["tool"], outcome["detail"],
                        status=OK if outcome["ok"] else FAILED)
        report = doctor_module.check(runner=runner, python=args.python)
        outstanding = doctor_module.missing(report)

    boards = []
    if any(entry["tool"] == "mpremote" and entry["found"] for entry in report):
        boards = discover_boards(runner=runner)
        result.step("boards", "{} answering{}".format(
            len(boards), (": " + ", ".join(boards)) if boards else ""),
            status=OK if boards else SKIPPED)

    result.set(python=sys.executable, tools=report, boards=boards, installed=installed)
    if outstanding:
        names = ", ".join(entry["tool"] for entry in outstanding)
        if args.install:
            result.fail("still missing after the install: " + names)
        else:
            result.fail("missing: {}. Re-run with --install to have this done for you, or use "
                        "the commands above.".format(names))
    return result


def preflight(args, result, runner=None):
    """Refuse to start a phase that would die halfway through for a missing tool.

    Checked here rather than discovered at the first call, because the calls that need these
    are not the first thing a phase does: a board can be probed, backed up and half configured
    before the flash finds that `esptool` was never there.
    """
    needed = ["mpremote"]
    if getattr(args, "firmware", None):
        needed.append("esptool")
    report = doctor_module.check(
        requirements=[r for r in doctor_module.REQUIREMENTS if r.key in needed], runner=runner)
    absent = doctor_module.missing(report)
    if not absent:
        return
    for entry in absent:
        result.note("{} is not on PATH: {}".format(entry["tool"], entry["why"]))
        _describe_plan(entry["plan"], result)
    raise BoardError(
        "{} missing. Run `provision doctor --install` to have this fixed, or use the command "
        "above.".format(", ".join(entry["tool"] for entry in absent)))


def cmd_setup(args, result, runner=None, sleep=None, ask=None, **_):
    return setup_module.run(args, result, runner=runner, sleep=sleep, ask=ask)


def cmd_apply(args, result, runner=None, sleep=None, **_):
    # Read before the preflight, because whether this machine needs `esptool` is something only
    # the plan knows: it is checked for when a plan names a firmware and not otherwise, and a
    # phase that dies halfway through for a missing tool leaves a half-configured board.
    doc = plan_module.read(args.plan)
    plan_module.require_bound(doc)
    if args.fleet is not None:
        doc["fleet"] = args.fleet
    # Any node naming an image is enough to need the flashing tool, since the run stops at the
    # first phase that cannot find it and a mixed pair routinely flashes one board and not the
    # other.
    args.firmware = next((e["firmware"] for e in [doc["hub"]] + doc["edges"] if e["firmware"]),
                         None)
    preflight(args, result, runner=runner)
    return setup_module.apply(doc, result, runner=runner, sleep=sleep)


COMMANDS = {
    "setup": cmd_setup,
    "apply": cmd_apply,
    "ports": cmd_ports,
    "doctor": cmd_doctor,
    "fleet-init": cmd_fleet_init,
    "fleet-show": cmd_fleet_show,
    "edge": cmd_edge,
    "hub": cmd_hub,
    "verify": cmd_verify,
}


def main(argv=None, runner=None, out=None, sleep=None, ask=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    result = Result(args.command, json_mode=args.json_mode)
    try:
        COMMANDS[args.command](args, result, runner=runner, sleep=sleep, ask=ask)
    except (BoardError, ValueError) as e:
        result.fail(str(e))
    return result.emit(out=out)


if __name__ == "__main__":
    sys.exit(main())

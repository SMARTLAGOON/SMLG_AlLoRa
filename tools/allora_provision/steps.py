"""The three phases: the Edge, the Hub, and the transfer that proves the pair.

The order inside each phase is not stylistic. Two steps in particular have to happen where they
happen, and both guard something that fails silently otherwise.

**The identity backup comes before the flash.** The flash erases the board filesystem and the
`device_id` is derived from a key that lives on it, so a reflash without a backup invalidates
the operator's registration and, on a bench board, the provenance of every measurement
published under that fingerprint. Nothing announces it: the board comes back working, with a
new identity, and the Hub simply never hears from the node it registered. This is the single
most damaging thing the wizard can get wrong, so it refuses to flash a board whose identity it
could not save.

**The device_id is read after the config lands, not before.** It does not exist until the node
runs with `identity_file` set, and asking for it earlier gets an answer that is either nothing
or a key from an older provisioning.

Phase 3 exists because without it "successfully provisioned" means only that files were copied.
"""
import json
import os
import threading

from AlLoRa.Digital_Endpoint import label_for_config
from tools.allora_provision.board import (PORT_GLOBS, Board, BoardError, candidate_ports,
                                          discover_boards)
from tools.allora_provision.fleet import CONTROL_ROOT_NAME, Fleet
from tools.allora_provision.node_config import (CONFIG_NAME, DEFAULT_BOARD, build_lora_json)
from tools.allora_provision.result import FAILED, OK, SKIPPED

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# The v3_hello payload, reused on purpose: what the wizard proves on a fresh deployment is the
# transfer the bench has already run many times, not a new one nobody has seen fail.
PAYLOAD_NAME = "hello.bin"
PAYLOAD_LEN = 1000

RADIOS = {
    "sx127x": "SX127x_connector",
    "sx1262": "SX1262_connector",
    "e5": "E5_connector",
    "lopy4": "LoPy4_connector",
}


def render_template(name, radio="sx127x", window=120,
                    payload_len=PAYLOAD_LEN, payload_name=PAYLOAD_NAME, edge_name="",
                    edge_device_id=""):
    """A board-side script with its radio and its bounds filled in.

    Token substitution rather than `str.format`, because the templates are Python and Python is
    full of braces. They stay valid, readable, runnable files on disk this way, which is what
    lets somebody debug one by pushing it to a board by hand.
    """
    if radio not in RADIOS:
        raise ValueError("unknown radio '{}'; known: {}".format(radio, ", ".join(sorted(RADIOS))))
    with open(os.path.join(_TEMPLATE_DIR, name), "r") as f:
        text = f.read()
    connector = RADIOS[radio]
    for token, value in (("__RADIO_MODULE__", connector),
                         ("__RADIO_CLASS__", connector),
                         ("__WINDOW__", str(window)),
                         ("__PAYLOAD_LEN__", str(payload_len)),
                         ("__PAYLOAD_NAME__", payload_name),
                         # A whole literal rather than the text between two quotes: a node is
                         # named by a free-text `--name`, and one apostrophe in it would
                         # otherwise push a syntax error onto the board.
                         ("__EDGE_NAME__", json.dumps(edge_name)),
                         ("__EDGE_DEVICE_ID__", json.dumps(edge_device_id))):
        text = text.replace(token, value)
    return text


def _write(path, text):
    # Bytes go out untouched: the verify payload is a file the Hub compares byte for byte, and
    # a text-mode write would translate line endings inside it on some hosts.
    mode = "wb" if isinstance(text, (bytes, bytearray)) else "w"
    with open(path, mode) as f:
        f.write(text)
    return path


def _stage(fleet, role, name, text):
    """Lay a file the operator can read next to the fleet, then push that.

    Staging rather than pushing from a temporary file so that what went onto a board is still
    on disk afterwards: when a node misbehaves, the first question is what it was actually
    given, and an answer that no longer exists is no answer.
    """
    target_dir = os.path.join(fleet.staging_path, role)
    if not os.path.isdir(target_dir):
        os.makedirs(target_dir)
    return _write(os.path.join(target_dir, name), text)


def back_up_identity(board, fleet, result, label, allow_identity_loss=False):
    """Save the board's identity before anything erases it. Returns the backup path or None.

    None means the board has never made one, which is the normal state of a board being
    provisioned for the first time and not a failure. A board that could not be *asked* is a
    different thing entirely and stops the phase, because the alternative is to erase a key
    on the guess that there was none: the board comes back working, with a new identity, and
    the Hub simply never hears from the node it registered.
    """
    try:
        material = board.identity_material()
    except BoardError as e:
        if not allow_identity_loss:
            raise BoardError(
                "{}. Flashing next would erase that file, and if the board did hold an "
                "identity its device_id would change with nothing saying so: every "
                "registration naming the old one would quietly stop matching. Fix the read, "
                "or pass --allow-identity-loss if this board's identity is genuinely "
                "disposable.".format(e))
        result.warn("could not read the board's identity ({}), flashing anyway because "
                    "--allow-identity-loss was given".format(e))
        return None
    if material is None:
        result.step("identity backup", "the board has none yet", status=SKIPPED)
        return None
    path = fleet.backup_identity(label, material)
    device_id = fleet.device_id_of_identity_file(path)
    result.step("identity backup", "{} (device_id {})".format(path, device_id[:16] + "..."))
    return path


def flash(board, fleet, result, firmware, label, allow_identity_loss=False,
          expected_mac=None, on_stall=None):
    """Erase and write the firmware, having first saved what the erase would destroy.

    `on_stall(port)` is how a caller with a person at the keyboard offers the tap on RESET
    without the flash having to know whether there is one. Given none -- which is every
    non-interactive command, and the website -- the wait ends in the same refusal it always
    did.
    """
    if not firmware:
        result.step("flash", "no firmware given, keeping what the board runs", status=SKIPPED)
        return True
    if not os.path.exists(firmware):
        raise BoardError("no firmware image at {}".format(firmware))

    back_up_identity(board, fleet, result, label, allow_identity_loss=allow_identity_loss)

    # Which board this is, asked while it can still be asked. The erase takes the filesystem
    # but not the MAC, so this is the one label that survives the flash and still means the
    # same board afterwards. Every port decision below is made against it rather than against
    # a count of who is answering.
    if expected_mac is None:
        expected_mac = board.mac()

    board.enter_bootloader()
    board.flash(firmware)
    result.step("flash", os.path.basename(firmware))

    wait_for_board(board, result, expected_mac, on_stall=on_stall)
    result.step("reboot", "the REPL answers on " + board.port)
    return True


# The post-flash wait, in rounds. Short ones, because on this hardware the board coming back
# by itself is the exception rather than the rule: the measured cost of a round is roughly its
# probe count times `POST_FLASH_PROBE`, and every second of it is a second the operator spends
# looking at a terminal that has stopped saying anything.
POST_FLASH_PROBE = 8
_SETTLE_PROBES = 3
_TAP_PROBES = 6


def wait_for_board(board, result, expected_mac, on_stall=None):
    """Get the flashed board back, escalating only as far as it has to.

    Three rounds, cheapest first. It may simply return; a reset issued over the wire brings it
    back when the write's own reset did not take, which on this hardware is every time; and a
    tap on RESET is the last resort, asked for only when somebody is there to be asked.

    Native USB re-enumerates on a hard reset, so neither "the REPL answers here" nor "somebody
    answers somewhere" is by itself evidence that the board in front of us is the board we
    flashed. Every round is checked against the MAC, and the port is re-derived from it at the
    end rather than assumed.
    """
    def waiting(what):
        def on_attempt(number, total):
            result.note("      {} ({} of {}, up to {}s each)".format(
                what, number, total, POST_FLASH_PROBE))
        return on_attempt

    def answered(probes, what):
        return board.wait_for_repl(attempts=probes, delay=1.0,
                                   probe_timeout=POST_FLASH_PROBE,
                                   on_attempt=waiting(what))

    alive = answered(_SETTLE_PROBES, "waiting for the board to come back")

    if not alive:
        # The write already ended with `--after hard_reset` and the board is silent anyway.
        # A second esptool call issues a reset it does act on, and costs seconds rather than
        # a trip to the bench. Only ever aimed at a port that answered nothing: it puts a
        # board into the ROM loader, which would take a healthy one down.
        result.step("wake", "the board is silent; resetting it over the wire")
        board.wake()
        alive = answered(_SETTLE_PROBES, "waiting after the reset")

    if not alive and on_stall is not None:
        # The one human moment in a flash, and it happens inside the run rather than after it.
        # Nothing else can hold the port while the wizard is waiting on it, so a reset from a
        # second process is not an option: the operator's finger is.
        on_stall(board.port)
        alive = answered(_TAP_PROBES, "waiting after the tap")

    if alive and _is_expected(board, expected_mac):
        return board.port

    adopted = adopt_port(board.port, runner=board.runner, expected_mac=expected_mac)
    if adopted is None:
        raise BoardError(
            "the board did not come back on {} after the flash, and a reset over the wire did "
            "not bring it back either. Tap RESET once on the board and re-run: the flash "
            "itself completed, so it is the wait that failed and not the write.".format(
                board.port))
    if adopted != board.port:
        result.warn("the board came back on {} instead of {}: native USB re-enumerates on a "
                    "hard reset".format(adopted, board.port))
        board.port = adopted
    return board.port


def _is_expected(board, expected_mac):
    """Whether the board answering now is the one we set out to provision.

    An unknown expectation cannot be contradicted, so a board whose MAC could not be read
    before the flash keeps the old behaviour rather than failing every run on a port that
    cannot answer this question.
    """
    return expected_mac is None or board.mac() == expected_mac


def _clear_stale_root(board, result):
    """Take the control root off a board that is no longer meant to hold one.

    The config decides the posture, so a leftover key is inert rather than dangerous today:
    a node whose `LoRa.json` no longer names one does not load it. It still comes off. Key
    material left on a board outlives the reason it was put there, and the day some later
    config does name the file, the node re-arms against whichever root happened to be lying
    around rather than the one the operator meant.
    """
    if board.remove_remote(CONTROL_ROOT_NAME):
        result.step("stale control root", "removed from the board")
        return True
    return False


def provision_edge(board, fleet, result, posture="secure", firmware=None, name=None,
                   rf=None, session_id=None, radio="sx127x", allow_identity_loss=False,
                   on_stall=None, board_model=DEFAULT_BOARD):
    """Phase 1. Leaves the board provisioned and its `device_id` registered in the fleet."""
    fleet.ensure()
    if not board.alive():
        raise BoardError(
            "nothing answers a MicroPython REPL on {}. Plug in one board at a time and confirm "
            "which port it appeared on; other USB serial devices look the same from "
            "here.".format(board.port))
    mac = board.mac()
    result.step("port", "{} (MAC {})".format(board.port, mac or "unknown"))

    label = "edge"
    flash(board, fleet, result, firmware, label, allow_identity_loss=allow_identity_loss,
          expected_mac=mac, on_stall=on_stall)

    config = build_lora_json(role="edge", posture=posture, driver=radio, name=name, rf=rf,
                             session_id=session_id, board=board_model)
    config_path = _stage(fleet, "edge", CONFIG_NAME, json.dumps(config, indent=2) + "\n")
    board.write_remote(config_path, CONFIG_NAME)
    result.step(CONFIG_NAME, "{} posture, {} radio on a {}".format(posture, radio,
                                                                   board_model))

    if posture == "control":
        # The verifying half, and only ever the verifying half. The private scalar on a node
        # that only obeys is the fleet's signing key in the field, and the node refuses to boot
        # on it: a wizard that pushed it would be hiding a key compromise behind a working link.
        staged_root = fleet.stage_public_root()
        board.write_remote(staged_root, CONTROL_ROOT_NAME)
        root, _ = fleet.load_or_create_root()
        result.step("control root", "verifying half pinned, fingerprint {}".format(
            root.fingerprint().hex()[:16] + "..."))
        result.note(
            "This node now refuses unsigned in-band commands. That refusal is the point of "
            "provisioning: if an unsigned frame still worked, the signature would protect "
            "nothing.")
    else:
        _clear_stale_root(board, result)

    # After the config, because the key does not exist until a node runs with identity_file set.
    device_id = None
    if posture != "open":
        device_id = board.ensure_identity()
        result.step("device_id", device_id)

    # The deployed program, the same file for every node in every posture on every radio. The
    # wizard invents nothing: this is the tracked example, and it is a node's program because
    # the config beside it says so.
    #
    # It is also what closes the radio bug. The Edge used to get a per-posture example whose
    # first import named SX127x_connector, so an operator who asked for an SX1262 Edge got an
    # SX127x one, and verify passed because both boards were provisioned the same wrong way.
    # There is no longer a program to hardcode a radio in.
    main_path = _stage(fleet, "edge", "main.py", _generic_main())
    board.write_remote(main_path, "main.py")
    result.step("main.py", "the generic program, radio from {}".format(CONFIG_NAME))

    # Something for the node to send. An Edge serves the files in its outbound folder and waits
    # quietly when there are none, so a deployment provisioned with an empty queue is correct
    # and silent, which is indistinguishable at the antenna from one that is broken.
    queue_path = config["queue_path"]
    board.mkdir_remote(queue_path)
    payload_path = _stage(fleet, "edge", PAYLOAD_NAME,
                          bytes((i % 256) for i in range(PAYLOAD_LEN)))
    board.write_remote(payload_path, queue_path + "/" + PAYLOAD_NAME)
    result.step(PAYLOAD_NAME, "{} bytes queued in {}".format(PAYLOAD_LEN, queue_path))

    # An open node is addressed by the session id its config carries, a secure one by the
    # fingerprint the board proved it holds. Registering an open node with an empty device_id
    # would be neither: the Hub reads it as a zero-length identity and raises at boot.
    entry = fleet.register(name=config["name"], role="edge",
                           device_id=device_id, posture=posture,
                           session_id=config.get("session_id"),
                           port=board.port, mac=mac, radio=radio, on_notice=result.warn,
                           connector={k: config["connector"][k]
                                      for k in ("freq", "sf", "bandwidth", "coding_rate",
                                                "tx_power")})
    result.step("registered", "{} in {}".format(entry["name"], fleet.registry_path))

    # Restart into what was just pushed. Without this the board is provisioned on disk and
    # still running whatever it ran before, which is the difference between a node that is set
    # up and a node that is working.
    if board.soft_reset():
        result.step("restart", "running the new main.py")
    else:
        result.warn("the board did not answer after the restart. Tap RESET once; the files are "
                    "already on it, so nothing needs re-running.")
    result.set(role="edge", port=board.port, posture=posture, device_id=device_id,
               fleet=fleet.path, config=config)
    return result


def provision_hub(board, fleet, result, posture="secure", firmware=None, name=None,
                  rf=None, session_id=None, radio="sx127x", on_site_root=False,
                  allow_identity_loss=False, on_stall=None, board_model=DEFAULT_BOARD):
    """Phase 2. Same discovery, same backup, same flash, plus the roster the Hub polls."""
    fleet.ensure()
    if not board.alive():
        raise BoardError(
            "nothing answers a MicroPython REPL on {}. Plug in one board at a time and confirm "
            "which port it appeared on.".format(board.port))
    mac = board.mac()
    result.step("port", "{} (MAC {})".format(board.port, mac or "unknown"))

    edges = [e for e in fleet.entries() if e.get("role") == "edge"]
    if posture != "open":
        edges = [e for e in edges if e.get("device_id")]
    if not edges:
        raise BoardError(
            "no Edge is registered in {} yet. The Hub needs the Edge's device_id and the Edge "
            "needs nothing about the Hub, so registration only runs one way: provision the "
            "Edge first.".format(fleet.registry_path))

    flash(board, fleet, result, firmware, "hub", allow_identity_loss=allow_identity_loss,
          expected_mac=mac, on_stall=on_stall)

    config = build_lora_json(role="hub", posture=posture, driver=radio, name=name, rf=rf,
                            session_id=session_id, on_site_root=on_site_root,
                            board=board_model)
    config_path = _stage(fleet, "hub", CONFIG_NAME, json.dumps(config, indent=2) + "\n")
    board.write_remote(config_path, CONFIG_NAME)
    result.step(CONFIG_NAME, "{} posture, {} radio on a {}".format(posture, radio,
                                                                   board_model))

    roster_path = _stage(fleet, "hub", "Nodes.json", fleet.render_nodes_json())
    board.write_remote(roster_path, "Nodes.json")
    result.step("Nodes.json", "{} edge(s) registered".format(len(edges)))

    if posture == "control":
        if on_site_root:
            staged_root = fleet.stage_private_root()
            board.write_remote(staged_root, CONTROL_ROOT_NAME)
            result.step("control root", "signing half on the board (on-site root)")
            result.warn(
                "On-site root: this machine is now the authority, not a courier. A compromised "
                "Hub can create commands and not only relay them, and the deployment no longer "
                "matches the trust model the paper describes. It is a supported mode; it is not "
                "the default one.")
            result.warn(
                "Exactly one signer per root. This Hub now mints under the fleet root, so the "
                "operator's machine must stop: two signers both start at zero, both mint number "
                "one, and the second artifact is refused as a replay, which looks exactly like "
                "the command not working.")
        else:
            result.step("control root", "none on the Hub: it carries artifacts, it does not "
                                        "mint them", status=SKIPPED)
            _clear_stale_root(board, result)
    else:
        _clear_stale_root(board, result)

    device_id = None
    if posture != "open":
        device_id = board.ensure_identity()
        result.step("device_id", device_id)

    # The same generic program the Edge got. Which of the two this board becomes is decided by
    # the "node" key in its config, not by which file was pushed.
    main_path = _stage(fleet, "hub", "main.py", _generic_main())
    board.write_remote(main_path, "main.py")
    result.step("main.py", "the generic program, registration read from Nodes.json")

    # The MAC is recorded alongside the port because it is the half that survives: a later run
    # extending this deployment has to know which board on the desk is this fleet's Hub, and
    # the port it answered on today says nothing about that tomorrow.
    entry = fleet.register(name=config["name"], role="hub", device_id=device_id,
                           posture=posture, port=board.port, mac=mac, radio=radio,
                           on_site_root=on_site_root, on_notice=result.warn)
    result.step("registered", "{} in {}".format(entry["name"], fleet.registry_path))

    # Restart into what was just pushed. Without this the board is provisioned on disk and
    # still running whatever it ran before, which is the difference between a node that is set
    # up and a node that is working.
    if board.soft_reset():
        result.step("restart", "running the new main.py")
    else:
        result.warn("the board did not answer after the restart. Tap RESET once; the files are "
                    "already on it, so nothing needs re-running.")
    result.set(role="hub", port=board.port, posture=posture, device_id=device_id,
               on_site_root=on_site_root, edges=len(edges), fleet=fleet.path, config=config)
    return result


def _rf_summary(entry):
    """The radio settings a roster entry states, in the words the wizard asks for them."""
    block = entry.get("connector") or {}
    stated = ["{} {}".format(key, block[key]) for key in ("sf", "freq", "bandwidth")
              if block.get(key) is not None]
    return ", ".join(stated) if stated else "no radio settings of its own"


def update_hub_roster(board, fleet, result, names):
    """Add or replace the rows for the Edges this run provisioned, and touch nothing else.

    This is the phase that extends a deployment already in the field. Adding an Edge changes
    one row in one file, and the full Hub phase rewrites four and restarts the board to do it,
    which on a live deployment costs more than the change is worth.

    **What it exists to protect is the rows it does not write.** `Nodes.json` is a read/write
    state file: when a retune is accepted, `Hub._persist_endpoint_rf` writes that endpoint's
    settled radio settings back into it. So the Hub's copy, not the operator's record, is the
    truth about what an Edge in the field is listening on. Rebuilding the roster from the fleet
    registry would put the Hub back on the settings that Edge was provisioned with a year ago
    and has since left, and nothing on either side would say so: the Hub simply polls an
    address nobody answers on.

    Rows are matched the way the Hub matches its own, by `label_for_config`, so an entry found
    here is the entry the Hub would have found. `names` are the fleet's names for the Edges
    this run provisioned; every other row is carried across exactly as it was read.
    """
    existing = board.read_remote("Nodes.json")
    if existing is None:
        raise BoardError(
            "the Hub on {} holds no Nodes.json, so there is no roster to extend. That is a Hub "
            "this toolkit has not provisioned: run the full Hub phase once to give it one, and "
            "extend it after that.".format(board.port))
    try:
        roster = json.loads(existing)
    except ValueError as e:
        raise BoardError(
            "the Nodes.json on the Hub at {} is not valid JSON ({}). Rewriting it from the "
            "fleet registry would drop whatever the Hub has settled on since it was "
            "provisioned, so this stops instead: look at the file, or run the full Hub "
            "phase to replace it deliberately.".format(board.port, e))
    if not isinstance(roster, list):
        raise BoardError(
            "the Nodes.json on the Hub at {} is not a list of entries. Run the full Hub phase "
            "to replace it deliberately.".format(board.port))

    incoming = [entry for entry in json.loads(fleet.render_nodes_json())
                if entry.get("name") in names]
    if not incoming:
        raise BoardError(
            "none of the Edges this run provisioned ({}) is registered in {}. The roster is "
            "built from that record, so there is nothing to write.".format(
                ", ".join(sorted(names)) or "none", fleet.registry_path))

    by_label = {label_for_config(entry): index for index, entry in enumerate(roster)}
    added, replaced = [], []
    for entry in incoming:
        label = label_for_config(entry)
        index = by_label.get(label)
        if index is None:
            # A fingerprint this Hub has never seen. Either a genuinely new node, or one whose
            # identity this toolkit rotated by flashing it, and the second leaves the old row
            # behind: the Hub would spend a listening window every cycle polling a node that
            # cannot answer. Same name means same slot, so the stale row goes.
            stale = [i for i, old in enumerate(roster) if old.get("name") == entry["name"]]
            for i in reversed(stale):
                dropped = roster.pop(i)
                result.warn(
                    "'{}' was in this Hub's roster as {} and now holds {}. Its old entry is "
                    "removed rather than left beside the new one: a Hub polling a fingerprint "
                    "nobody holds spends a listening window on it every cycle.".format(
                        entry["name"], label_for_config(dropped)[:8], label[:8]))
            roster.append(entry)
            added.append(entry["name"])
        else:
            roster[index] = entry
            replaced.append(entry["name"])
        by_label = {label_for_config(e): i for i, e in enumerate(roster)}

    written = set(entry["name"] for entry in incoming)
    kept = [entry for entry in roster if entry.get("name") not in written]
    for entry in kept:
        result.step("kept", "{} on {}, as this Hub has it".format(
            entry.get("name", "?"), _rf_summary(entry)), status=SKIPPED)

    roster_path = _stage(fleet, "hub", "Nodes.json", json.dumps(roster, indent=2) + "\n")
    board.write_remote(roster_path, "Nodes.json")
    result.step("Nodes.json", "{} row(s) written, {} left as the Hub has them".format(
        len(incoming), len(kept)))
    if kept:
        result.note(
            "The rows above were not rewritten. This Hub edits its own roster when a node "
            "accepts a retune, so its copy is what those Edges are actually listening on, and "
            "the fleet registry is only what they were issued.")
    # A Hub registers its endpoints once, from the file it reads at boot, so a roster written
    # under a running Hub changes nothing until it restarts. The restart is also what closes
    # the read-modify-write window: `mpremote` interrupts the running program to reach the
    # filesystem, so the Hub is stopped from the read through to the write and cannot settle a
    # trial into the copy being replaced.
    if board.soft_reset():
        result.step("restart", "polling the roster it was just given")
    else:
        result.warn("the Hub did not answer after the restart. The roster is already written, "
                    "so tap RESET once and it will register from it; until then the Hub is "
                    "still polling the roster it booted with.")
    result.set(role="hub", port=board.port, fleet=fleet.path,
               added=added, replaced=replaced,
               kept=[entry.get("name") for entry in kept], roster=roster)
    return result


def parse_verify_lines(text):
    """The VERIFY: facts a board printed, as a dict. Everything else is ignored."""
    facts = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("VERIFY:"):
            continue
        parts = line[len("VERIFY:"):].split(None, 1)
        if parts:
            facts[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return facts


def verify_pair(edge_board, hub_board, fleet, result, posture="secure", radio="sx127x",
                window=120):
    """Phase 3. Drive one real transfer and check what actually happened.

    Three questions, and all three have to be asked. Did the pair complete a transfer; is the
    posture the one that was asked for; are the received bytes the bytes that were sent. A
    "provisioned" message that skips these means only that files were copied.

    Both sides run under `mpremote run`, which soft-resets: the port does not re-enumerate, so
    the output is caught from the first line. They run at once because one of them has to be
    listening while the other talks, and the Edge starts first so the Hub's first poll finds
    somebody there.
    """
    # The radio each board was actually provisioned with, not the one this command was told.
    # `allora verify` defaults its flag to sx127x, so verifying a pair provisioned as sx1262
    # would drive the wrong driver on both boards and report a link failure. The registry is the
    # record of what was written, so it answers instead, and the flag is the fallback for a pair
    # this machine did not provision.
    edge_radio = _registered_radio(fleet, "edge", radio, result)
    hub_radio = _registered_radio(fleet, "hub", radio, result)

    edge_script = _stage(fleet, "verify", "verify_edge.py",
                         render_template("verify_edge.py", radio=edge_radio, window=window))
    # Which Edge the Hub half checks. Without this it takes `digital_endpoints[0]`, the oldest
    # entry in the roster, so on a Hub holding more than one Edge the proof step reports on a
    # node nobody asked about, and on a roster still carrying a stale entry it reported on the
    # dead one by construction rather than by luck.
    edge_entry = _registered_edge(fleet, "edge", edge_board)
    if edge_entry is None:
        result.warn("this fleet has no registry entry for the Edge on {}, so the Hub will "
                    "verify against the first node in its roster.".format(edge_board.port))
    hub_script = _stage(fleet, "verify", "verify_hub.py",
                        render_template("verify_hub.py", radio=hub_radio, window=window,
                                        edge_name=(edge_entry or {}).get("name", ""),
                                        edge_device_id=(edge_entry or {}).get("device_id", "")))

    outputs = {}

    def _run(key, board, script):
        try:
            outputs[key] = board.run_script(script, timeout=window + 120)
        except BoardError as e:
            outputs[key] = (1, "", str(e))

    edge_thread = threading.Thread(target=_run, args=("edge", edge_board, edge_script))
    hub_thread = threading.Thread(target=_run, args=("hub", hub_board, hub_script))
    edge_thread.start()
    hub_thread.start()
    edge_thread.join()
    hub_thread.join()

    edge_facts = parse_verify_lines(outputs.get("edge", (1, "", ""))[1])
    hub_facts = parse_verify_lines(outputs.get("hub", (1, "", ""))[1])
    result.set(edge=edge_facts, hub=hub_facts)

    expected_mode = "open" if posture == "open" else "secure"
    checks = []

    def check(name, passed, detail):
        checks.append({"check": name, "ok": bool(passed), "detail": detail})
        result.step(name, detail, status=OK if passed else FAILED)

    check("posture", hub_facts.get("mode") == expected_mode and
          edge_facts.get("mode") == expected_mode,
          "edge {} / hub {}, asked for {}".format(
              edge_facts.get("mode", "?"), hub_facts.get("mode", "?"), expected_mode))

    if expected_mode == "secure":
        # A configured-secure node halts rather than running plaintext, so a backend on both
        # ends is what says the frames are sealed and not merely meant to be.
        check("sealed", hub_facts.get("aead") == "yes" and hub_facts.get("session") == "yes",
              "aead {}, live session {}".format(hub_facts.get("aead", "?"),
                                                hub_facts.get("session", "?")))

    check("transfer", hub_facts.get("result") == "pass",
          "hub reports {}".format(hub_facts.get("result", "nothing")))
    check("bytes intact", hub_facts.get("intact") == "yes",
          "{} bytes received".format(hub_facts.get("bytes", "0")))

    result.set(checks=checks)
    failed = [c["check"] for c in checks if not c["ok"]]
    if failed:
        result.fail("verification failed: " + ", ".join(failed))
    return result


def _registered_edge(fleet, role, board):
    """This fleet's entry for the board on this port, or None if it registered none.

    By port rather than by position, because the point is to find the board physically in front
    of the operator. A fleet this machine did not provision has no entry, and None says so: the
    caller falls back to whatever the roster leads with rather than aiming at a node picked by
    a rule that was never true.
    """
    try:
        entries = [e for e in fleet.entries()
                   if e.get("role") == role and e.get("port") == board.port]
    except Exception:
        return None
    return entries[-1] if entries else None


def _registered_radio(fleet, role, fallback, result):
    """The radio this role was provisioned with, per the fleet registry.

    Falls back to what the caller asked for when the registry has no entry, which is the case
    for a pair provisioned somewhere else. A disagreement is reported rather than resolved
    silently: it means the boards in front of the operator are not the ones this fleet
    describes, and that is worth knowing before a failed transfer is blamed on the antenna.
    """
    try:
        entries = [e for e in fleet.entries() if e.get("role") == role and e.get("radio")]
    except Exception:
        return fallback
    if not entries:
        return fallback
    registered = entries[-1]["radio"]
    if registered != fallback:
        result.warn(
            "the {} is registered as {} and this run was told {}. Verifying on {}, which is "
            "what it was provisioned with.".format(role, registered, fallback, registered))
    return registered


def _generic_main():
    """The tracked v3_hello program every provisioned node runs.

    One file for both placements and all three postures, because what a board is now comes from
    its config rather than from which program was copied onto it. Pushing the tracked file
    rather than a template of the wizard's own keeps one program to keep true, and keeps the
    deployment a reader can reproduce by hand identical to the one the wizard produces.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(_TEMPLATE_DIR)))
    path = os.path.join(repo_root, "examples", "v3_hello", "main.py")
    if not os.path.exists(path):
        raise BoardError(
            "examples/v3_hello/main.py is missing. The wizard pushes the tracked program "
            "rather than one of its own, so there is nothing to provision a node with.")
    with open(path, "r") as f:
        return f.read()


def adopt_port(previous_port, runner=None, expected_mac=None, globs=PORT_GLOBS):
    """The port a given board answers on now, which a hard reset may have changed.

    Native USB re-enumerates, so the path a board was flashed on is not reliably the path it
    comes back on. `expected_mac` is what makes the search safe: it names the board being
    looked for, so the answer is "the port this board is on" rather than "the port some board
    is on", and any number of other boards may be connected without changing it.

    Counting was tried first and is wrong in exactly the case that matters. The rule was "when
    exactly one board answers, that is the one", but a board answers only once its REPL is
    back, and immediately after a flash the freshly written board is precisely the one that
    cannot answer yet. So a second connected board is the only one answering, the count is one,
    and it is adopted and provisioned in place of the board the operator named. Observed on the
    bench on 2026-08-27: an Edge was flashed and the Hub received its config, its control root
    and its `main.py`, with every step reported ok.

    Without a MAC there is nothing to match, so the old rule stands, narrowed: it applies only
    when the machine has a single candidate port at all, which is the one arrangement where
    there is no other board to confuse it with. Note that a candidate is any port that looks
    like a board, not any port that answers, so an unrelated USB serial device on the desk is
    enough to make the wizard ask rather than guess.
    """
    answering = discover_boards(runner=runner, globs=globs)
    if expected_mac is not None:
        # The previous port first: it is the likeliest answer and re-enumeration is the
        # exception, so the common case costs one probe rather than one per connected board.
        ordered = ([previous_port] if previous_port in answering else []
                   ) + [port for port in answering if port != previous_port]
        for port in ordered:
            if Board(port, runner=runner).mac() == expected_mac:
                return port
        return None
    if previous_port in answering:
        return previous_port
    if len(answering) == 1 and len(candidate_ports(globs)) == 1:
        return answering[0]
    return None

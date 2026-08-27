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

from tools.allora_provision.board import (PORT_GLOBS, Board, BoardError, candidate_ports,
                                          discover_boards)
from tools.allora_provision.fleet import CONTROL_ROOT_NAME, Fleet
from tools.allora_provision.node_config import build_lora_json
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
                    payload_len=PAYLOAD_LEN, payload_name=PAYLOAD_NAME):
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
                         ("__PAYLOAD_NAME__", payload_name)):
        text = text.replace(token, value)
    return text


def _write(path, text):
    with open(path, "w") as f:
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
          expected_mac=None):
    """Erase and write the firmware, having first saved what the erase would destroy."""
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

    # Native USB re-enumerates on a hard reset, and the write ends in one, so neither "the
    # REPL answers here" nor "somebody answers somewhere" is by itself evidence that the board
    # in front of us is the board we flashed. Both are checked against the MAC.
    if not (board.wait_for_repl() and _is_expected(board, expected_mac)):
        adopted = adopt_port(board.port, runner=board.runner, expected_mac=expected_mac)
        if adopted is None or adopted == board.port:
            raise BoardError(
                "the board did not come back on {} after the flash. On native USB the port "
                "re-enumerates, and the REPL is often unreachable until somebody taps RESET "
                "once. Tap it and re-run.".format(board.port))
        result.warn("the board came back on {} instead of {}: native USB re-enumerates on a "
                    "hard reset".format(adopted, board.port))
        board.port = adopted
    result.step("reboot", "the REPL answers on " + board.port)
    return True


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
                   rf=None, session_id=None, radio="sx127x", allow_identity_loss=False):
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
          expected_mac=mac)

    config = build_lora_json(role="edge", posture=posture, name=name, rf=rf,
                             session_id=session_id)
    config_path = _stage(fleet, "edge", "LoRa.json", json.dumps(config, indent=2) + "\n")
    board.write_remote(config_path, "LoRa.json")
    result.step("LoRa.json", "{} posture".format(posture))

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

    # The Edge's deployed program is the example for its posture, unchanged. The wizard has no
    # reason to invent a second one: the examples are what the bench has run, they guard their
    # own posture, and a second Edge program would be a second thing to keep true.
    example = _example_main("edge", posture)
    if example is None:
        raise BoardError(
            "no Edge example for the {} posture under examples/v3_hello. The wizard pushes the "
            "tracked example rather than a program of its own.".format(posture))
    main_path = _stage(fleet, "edge", "main.py", example)
    board.write_remote(main_path, "main.py")
    result.step("main.py", "the {} example's Edge".format(posture))

    # An open node is addressed by the session id its config carries, a secure one by the
    # fingerprint the board proved it holds. Registering an open node with an empty device_id
    # would be neither: the Hub reads it as a zero-length identity and raises at boot.
    entry = fleet.register(name=config["name"], role="edge",
                           device_id=device_id, posture=posture,
                           session_id=config.get("session_id"),
                           port=board.port, radio=radio,
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
                  allow_identity_loss=False):
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
          expected_mac=mac)

    config = build_lora_json(role="hub", posture=posture, name=name, rf=rf,
                            session_id=session_id, on_site_root=on_site_root)
    config_path = _stage(fleet, "hub", "LoRa.json", json.dumps(config, indent=2) + "\n")
    board.write_remote(config_path, "LoRa.json")
    result.step("LoRa.json", "{} posture".format(posture))

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

    main_path = _stage(fleet, "hub", "main.py", render_template("hub_main.py", radio=radio))
    board.write_remote(main_path, "main.py")
    result.step("main.py", "registration read from Nodes.json")

    entry = fleet.register(name=config["name"], role="hub", device_id=device_id,
                           posture=posture, port=board.port, radio=radio,
                           on_site_root=on_site_root)
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
    edge_script = _stage(fleet, "verify", "verify_edge.py",
                         render_template("verify_edge.py", radio=radio, window=window))
    hub_script = _stage(fleet, "verify", "verify_hub.py",
                        render_template("verify_hub.py", radio=radio, window=window))

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


def _example_main(role, posture):
    """The tracked v3_hello program for this placement and posture, if there is one."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(_TEMPLATE_DIR)))
    path = os.path.join(repo_root, "examples", "v3_hello", posture, role, "main.py")
    if not os.path.exists(path):
        return None
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

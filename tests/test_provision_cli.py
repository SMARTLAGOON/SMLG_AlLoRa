"""The wizard end to end, with a fake wire in place of two boards.

The criterion the whole tool is judged on is written down in ADR 0008 and ADR 0005: **one
value per node, copied once**. If an operator ends up handling two values for one node, the
wizard has failed a decision already made, however smooth it feels. So the flow test below
provisions an Edge and then a Hub without the Hub command ever being told a `device_id`: it
comes out of the fleet registry, which is the point of registering there rather than pasting a
constant into `main.py`.

The rest pins down the things that are silent when they go wrong: the private half never
reaching a node that only verifies, the identity backup happening before the erase that would
destroy it, and `--json` putting exactly one document on stdout.
"""
import io
import json
import os

import pytest

from tools.allora_provision.cli import main
from tools.allora_provision.fleet import PRIVATE_HEX_LEN, PUBLIC_HEX_LEN

EDGE_PORT = "/dev/cu.usbmodem1101"
HUB_PORT = "/dev/cu.usbmodem2101"
EDGE_DEVICE_ID = "d909f4eb" + "11" * 28
HUB_DEVICE_ID = "a1b2c3d4" + "22" * 28


class FakeWire:
    """Two boards on the end of a pretend `mpremote`/`esptool`."""

    def __init__(self, identities=None, files=None):
        self.calls = []
        self.pushed = {}          # port -> {remote name: contents}
        self.identities = identities or {EDGE_PORT: EDGE_DEVICE_ID, HUB_PORT: HUB_DEVICE_ID}
        self.existing = files or {}
        self.verify_output = {}

    def run(self, argv, timeout=120, capture=True):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        port = next((p for p in (EDGE_PORT, HUB_PORT) if p in joined), None)

        if argv[0] == "esptool.py":
            return 0, "", ""
        if "ALLORA_PROBE" in joined:
            return 0, "ALLORA_PROBE\n", ""
        if "load_or_create_identity" in joined:
            return 0, "DEVID:{}\n".format(self.identities[port]), ""
        if "fs cat" in joined or ("fs" in argv and "cat" in argv):
            name = argv[-1]
            have = self.existing.get(port, {})
            return (0, have[name], "") if name in have else (1, "", "no such file")
        if "fs" in argv and "rm" in argv:
            name = argv[-1]
            if name in self.pushed.get(port, {}):
                del self.pushed[port][name]
                return 0, "", ""
            return 1, "", "OSError: [Errno 2] ENOENT"
        if "fs" in argv and "cp" in argv:
            local, remote = argv[-2], argv[-1].lstrip(":")
            with open(local) as f:
                self.pushed.setdefault(port, {})[remote] = f.read()
            return 0, "", ""
        if "run" in argv:
            return 0, self.verify_output.get(port, ""), ""
        return 0, "", ""

    def on(self, port, name):
        return self.pushed.get(port, {}).get(name)


def _run(argv, wire, fleet_dir):
    out = io.StringIO()
    code = main(list(argv) + ["--fleet", str(fleet_dir)], runner=wire, out=out,
                sleep=lambda _seconds: None)
    return code, out.getvalue()


def _json_run(argv, wire, fleet_dir):
    code, text = _run(list(argv) + ["--json"], wire, fleet_dir)
    return code, json.loads(text)


# --- the gesture the tool is judged on ---------------------------------------------------

def test_the_hub_is_never_told_a_device_id_by_hand(tmp_path):
    """One value per node, copied once. The Edge's fingerprint reaches the Hub through the
    fleet registry, and `provision hub` is given no identifier at all."""
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 0, edge
    assert edge["data"]["device_id"] == EDGE_DEVICE_ID

    code, hub = _json_run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    assert code == 0, hub

    roster = json.loads(wire.on(HUB_PORT, "Nodes.json"))
    assert [entry["device_id"] for entry in roster] == [EDGE_DEVICE_ID]
    assert "EDGE_DEVICE_ID" not in wire.on(HUB_PORT, "main.py")
    assert "Nodes.json" in wire.on(HUB_PORT, "main.py")


def test_a_hub_provisioned_before_any_edge_says_which_way_registration_runs(tmp_path):
    wire = FakeWire()
    code, hub = _json_run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    assert code == 1
    assert "provision the Edge first" in hub["error"]


def test_both_boards_get_the_radio_settings_that_have_to_match(tmp_path):
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT, "--sf", "10", "--freq", "867"], wire, tmp_path / "f")
    _run(["hub", "--port", HUB_PORT, "--sf", "10", "--freq", "867"], wire, tmp_path / "f")
    edge = json.loads(wire.on(EDGE_PORT, "LoRa.json"))
    hub = json.loads(wire.on(HUB_PORT, "LoRa.json"))
    for field in ("sf", "freq", "bandwidth", "coding_rate"):
        assert edge["connector"][field] == hub["connector"][field]
    assert edge["connector"]["sf"] == 10


# --- the control root --------------------------------------------------------------------

def test_the_commanded_node_gets_the_verifying_half_and_only_that(tmp_path):
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--posture", "control"],
                           wire, tmp_path / "fleet")
    assert code == 0, edge
    material = wire.on(EDGE_PORT, "control_root.key").strip()
    assert len(material) == PUBLIC_HEX_LEN
    assert json.loads(wire.on(EDGE_PORT, "LoRa.json"))["control_root_file"] == "control_root.key"


def test_the_hub_is_a_courier_by_default_and_holds_no_root(tmp_path):
    """The control root lives with the operator, not with the radio. Steal the Hub and you
    have a relay, not an authority, and no Edge needs revisiting."""
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT, "--posture", "control"], wire, tmp_path / "fleet")
    code, hub = _json_run(["hub", "--port", HUB_PORT, "--posture", "control"],
                          wire, tmp_path / "fleet")
    assert code == 0, hub
    assert wire.on(HUB_PORT, "control_root.key") is None
    assert "control_root_file" not in json.loads(wire.on(HUB_PORT, "LoRa.json"))


def test_the_on_site_mode_hands_over_the_signing_half_and_says_what_it_costs(tmp_path):
    """A real mode, not an escape hatch: it gets a name, a config and documentation. What it
    does not get is silence."""
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT, "--posture", "control"], wire, tmp_path / "fleet")
    code, hub = _json_run(["hub", "--port", HUB_PORT, "--posture", "control", "--on-site-root"],
                          wire, tmp_path / "fleet")
    assert code == 0, hub
    assert len(wire.on(HUB_PORT, "control_root.key").strip()) == PRIVATE_HEX_LEN
    warnings = " ".join(hub["warnings"])
    assert "relay" in warnings or "courier" in warnings or "authority" in warnings
    assert "one signer" in warnings.lower()


def test_the_fleet_root_is_minted_once_and_reused_by_both_boards(tmp_path):
    wire = FakeWire()
    fleet_dir = tmp_path / "fleet"
    code, first = _json_run(["fleet-init"], wire, fleet_dir)
    assert code == 0 and first["data"]["created"] is True
    code, again = _json_run(["fleet-init"], wire, fleet_dir)
    assert again["data"]["created"] is False
    assert again["data"]["fingerprint"] == first["data"]["fingerprint"]

    _run(["edge", "--port", EDGE_PORT, "--posture", "control"], wire, fleet_dir)
    import hashlib
    pushed = wire.on(EDGE_PORT, "control_root.key").strip()
    assert hashlib.sha256(bytes.fromhex(pushed)).hexdigest() == first["data"]["fingerprint"]


# --- the identity backup -----------------------------------------------------------------

def test_a_board_with_an_identity_is_backed_up_before_the_flash_erases_it(tmp_path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x00")
    wire = FakeWire(files={EDGE_PORT: {"identity.key": "ab" * 32}})
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--firmware", str(firmware)],
                           wire, tmp_path / "fleet")
    assert code == 0, edge

    order = [" ".join(call) for call in wire.calls]
    backed_up = next(i for i, c in enumerate(order) if "fs cat identity.key" in c)
    erased = next(i for i, c in enumerate(order) if "erase_flash" in c)
    assert backed_up < erased

    backups = os.listdir(tmp_path / "fleet" / "backups")
    assert len(backups) == 1
    with open(tmp_path / "fleet" / "backups" / backups[0]) as f:
        assert f.read().strip() == "ab" * 32


def test_a_board_whose_identity_cannot_be_read_is_not_flashed(tmp_path):
    """A read that failed must never be reported as "there was nothing there". The board would
    come back working, with a new identity, and the Hub would simply never hear from the node
    it registered. Nothing announces that, so the wizard stops."""
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x00")

    class _BusyPort(FakeWire):
        def run(self, argv, timeout=120, capture=True):
            if "fs" in argv and "cat" in argv:
                self.calls.append(list(argv))
                return 1, "", "could not enter raw repl: device busy"
            return super().run(argv, timeout=timeout, capture=capture)

    wire = _BusyPort()
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--firmware", str(firmware)],
                           wire, tmp_path / "fleet")
    assert code == 1
    assert "identity" in edge["error"] and "--allow-identity-loss" in edge["error"]
    assert not any("erase_flash" in " ".join(call) for call in wire.calls)


def test_the_refusal_can_be_overridden_for_a_board_whose_identity_is_disposable(tmp_path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x00")

    class _BusyPort(FakeWire):
        def run(self, argv, timeout=120, capture=True):
            if "fs" in argv and "cat" in argv:
                self.calls.append(list(argv))
                return 1, "", "could not enter raw repl: device busy"
            return super().run(argv, timeout=timeout, capture=capture)

    wire = _BusyPort()
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--firmware", str(firmware),
                            "--allow-identity-loss"], wire, tmp_path / "fleet")
    assert code == 0, edge
    assert any("erase_flash" in " ".join(call) for call in wire.calls)
    assert any("identity" in w for w in edge["warnings"])


def test_a_fresh_board_is_not_told_a_stale_key_was_removed(tmp_path):
    """`mpremote fs rm` fails on a file that is not there, and a step reported for a removal
    that did not happen is a line the operator has to reason about for nothing."""
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 0
    assert not any(step["step"] == "stale control root" for step in edge["steps"])


def test_a_first_time_board_has_nothing_to_back_up_and_says_so(tmp_path):
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x00")
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--firmware", str(firmware)],
                           wire, tmp_path / "fleet")
    assert code == 0
    backup = next(s for s in edge["steps"] if s["step"] == "identity backup")
    assert backup["status"] == "skipped"


def test_no_firmware_means_the_board_keeps_what_it_runs(tmp_path):
    """Both bench boards already run the current library tip. A reflash is needed only when a
    library change lands, so it is not the default."""
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 0
    assert not any(call[0] == "esptool.py" for call in wire.calls)
    flash = next(s for s in edge["steps"] if s["step"] == "flash")
    assert flash["status"] == "skipped"


# --- discovery ---------------------------------------------------------------------------

def test_a_port_is_never_assumed_when_two_boards_answer(tmp_path, monkeypatch):
    import tools.allora_provision.board as board_module
    monkeypatch.setattr(board_module.glob, "glob",
                        lambda _p: [EDGE_PORT, HUB_PORT] if "usbmodem" in _p else [])
    wire = FakeWire()
    code, edge = _json_run(["edge"], wire, tmp_path / "fleet")
    assert code == 1
    assert "--port" in edge["error"]
    assert EDGE_PORT in edge["error"] and HUB_PORT in edge["error"]


def test_one_answering_board_is_adopted_without_being_named(tmp_path, monkeypatch):
    import tools.allora_provision.board as board_module
    monkeypatch.setattr(board_module.glob, "glob",
                        lambda _p: [EDGE_PORT] if "usbmodem" in _p else [])
    wire = FakeWire()
    code, edge = _json_run(["edge"], wire, tmp_path / "fleet")
    assert code == 0
    assert edge["data"]["port"] == EDGE_PORT


# --- verify ------------------------------------------------------------------------------

_PASSING = ("VERIFY:mode secure\nVERIFY:aead yes\nVERIFY:endpoints 1\n"
            "VERIFY:label d909f4eb\nVERIFY:sid 217\nVERIFY:session yes\n"
            "VERIFY:bytes 1000\nVERIFY:intact yes\nVERIFY:result pass\n")
_EDGE_SIDE = "VERIFY:mode secure\nVERIFY:device_id {}\nVERIFY:result served\n".format(
    EDGE_DEVICE_ID)


def test_a_clean_run_passes_every_check(tmp_path):
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    _run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    wire.verify_output = {EDGE_PORT: _EDGE_SIDE, HUB_PORT: _PASSING}
    code, verify = _json_run(
        ["verify", "--edge-port", EDGE_PORT, "--hub-port", HUB_PORT, "--window", "5"],
        wire, tmp_path / "fleet")
    assert code == 0, verify
    assert all(check["ok"] for check in verify["data"]["checks"])
    assert {c["check"] for c in verify["data"]["checks"]} == {
        "posture", "sealed", "transfer", "bytes intact"}


def test_a_transfer_that_delivered_the_wrong_bytes_is_not_a_pass(tmp_path):
    """A transfer that completed and delivered something else is not a transfer that worked."""
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    _run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    wire.verify_output = {
        EDGE_PORT: _EDGE_SIDE,
        HUB_PORT: _PASSING.replace("intact yes", "intact no").replace("result pass",
                                                                     "result corrupt")}
    code, verify = _json_run(
        ["verify", "--edge-port", EDGE_PORT, "--hub-port", HUB_PORT, "--window", "5"],
        wire, tmp_path / "fleet")
    assert code == 1
    assert "bytes intact" in verify["error"]


def test_a_secure_run_that_never_sealed_anything_fails_the_posture_it_claimed(tmp_path):
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    _run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    wire.verify_output = {EDGE_PORT: _EDGE_SIDE,
                          HUB_PORT: _PASSING.replace("session yes", "session no")}
    code, verify = _json_run(
        ["verify", "--edge-port", EDGE_PORT, "--hub-port", HUB_PORT, "--window", "5"],
        wire, tmp_path / "fleet")
    assert code == 1
    assert "sealed" in verify["error"]


def test_a_board_that_said_nothing_fails_rather_than_passing_quietly(tmp_path):
    wire = FakeWire()
    _run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    _run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    wire.verify_output = {EDGE_PORT: "", HUB_PORT: ""}
    code, verify = _json_run(
        ["verify", "--edge-port", EDGE_PORT, "--hub-port", HUB_PORT, "--window", "5"],
        wire, tmp_path / "fleet")
    assert code == 1


# --- the integration surface -------------------------------------------------------------

def test_json_mode_puts_exactly_one_document_on_stdout(tmp_path, capsys):
    """The website shells out to this. Progress on stdout would make every caller filter it."""
    wire = FakeWire()
    out = io.StringIO()
    code = main(["edge", "--port", EDGE_PORT, "--fleet", str(tmp_path / "fleet"), "--json"],
                runner=wire, out=out, sleep=lambda _seconds: None)
    assert code == 0
    document = json.loads(out.getvalue())
    assert set(document) == {"command", "ok", "error", "steps", "warnings", "data"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[  ok]" in captured.err


def test_every_command_takes_json(tmp_path):
    wire = FakeWire()
    for argv in (["ports"], ["fleet-init"], ["fleet-show"]):
        code, document = _json_run(argv, wire, tmp_path / "fleet")
        assert code == 0
        assert document["command"] == argv[0]


def test_a_failure_is_a_document_and_a_nonzero_exit(tmp_path):
    wire = FakeWire()
    code, document = _json_run(["hub", "--port", HUB_PORT], wire, tmp_path / "fleet")
    assert code == 1
    assert document["ok"] is False
    assert document["error"]


# --- the open posture --------------------------------------------------------------------

def test_an_open_pair_is_addressed_by_the_session_id_both_ends_carry(tmp_path):
    """An open node has no identity to be addressed by, which is the whole difference the
    posture makes. It must not be registered with an empty fingerprint."""
    wire = FakeWire()
    fleet_dir = tmp_path / "fleet"
    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--posture", "open",
                            "--session-id", "42"], wire, fleet_dir)
    assert code == 0, edge
    assert edge["data"]["device_id"] is None
    code, hub = _json_run(["hub", "--port", HUB_PORT, "--posture", "open",
                           "--session-id", "42"], wire, fleet_dir)
    assert code == 0, hub

    roster = json.loads(wire.on(HUB_PORT, "Nodes.json"))
    assert len(roster) == 1
    assert "device_id" not in roster[0]
    assert roster[0]["session_id"] == 42


def test_the_roster_the_wizard_wrote_boots_a_real_hub_in_either_posture(tmp_path):
    """The registry is only worth having if `Hub.add_digital_endpoints` takes it unedited. A
    zero-length device_id parses as hex and then raises when the sid is derived from it, so
    this is the check that the open path did not write one."""
    from AlLoRa.Connectors.Loopback_connector import Loopback_connector
    from AlLoRa.Nodes.Hub import Hub

    for posture, extra in (("secure", []), ("open", ["--session-id", "42"])):
        wire = FakeWire()
        fleet_dir = tmp_path / ("fleet-" + posture)
        assert _run(["edge", "--port", EDGE_PORT, "--posture", posture] + extra,
                    wire, fleet_dir)[0] == 0
        assert _run(["hub", "--port", HUB_PORT, "--posture", posture] + extra,
                    wire, fleet_dir)[0] == 0

        roster_path = str(tmp_path / ("Nodes-" + posture + ".json"))
        with open(roster_path, "w") as f:
            f.write(wire.on(HUB_PORT, "Nodes.json"))
        config_path = str(tmp_path / ("LoRa-" + posture + ".json"))
        config = json.loads(wire.on(HUB_PORT, "LoRa.json"))
        config["security_mode"] = "open"      # no radio here, so no handshake to run
        config["result_path"] = str(tmp_path / "Results")
        with open(config_path, "w") as f:
            json.dump(config, f)

        hub = Hub(Loopback_connector("b2b2b2b2"), config_file=config_path,
                  nodes_file=roster_path)
        assert len(hub.digital_endpoints) == 1


def test_dropping_out_of_the_control_posture_takes_the_key_off_the_board(tmp_path):
    """Key material left on a board outlives the reason it was put there. It is inert while the
    config does not name it, and it should not be waiting for a config that does."""
    wire = FakeWire()
    fleet_dir = tmp_path / "fleet"
    assert _run(["edge", "--port", EDGE_PORT, "--posture", "control"], wire, fleet_dir)[0] == 0
    assert wire.on(EDGE_PORT, "control_root.key") is not None

    code, edge = _json_run(["edge", "--port", EDGE_PORT, "--posture", "secure"],
                           wire, fleet_dir)
    assert code == 0, edge
    assert wire.on(EDGE_PORT, "control_root.key") is None
    assert any(step["step"] == "stale control root" for step in edge["steps"])


def test_the_control_posture_is_offered_and_explained_rather_than_assumed(tmp_path, capsys):
    """A node holding a control root refuses unsigned in-band commands from then on. That is a
    real change to what the deployment accepts, so it is chosen, not inherited: the default is
    secure, and the offer is made in words rather than left in a README."""
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 0
    assert "control_root_file" not in edge["data"]["config"]
    offered = capsys.readouterr().err
    assert "--posture control" in offered
    assert "refuses unsigned" in offered


# --- the tools the wizard shells out to --------------------------------------------------

def _no_tools(monkeypatch, present=()):
    """Make the machine look like one where only `present` is installed."""
    import shutil
    real = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: ("/opt/bin/" + name) if name in present
                        else None)
    return real


def test_doctor_passes_when_both_tools_are_there(tmp_path, monkeypatch):
    _no_tools(monkeypatch, present=("mpremote", "esptool.py"))
    wire = FakeWire()
    code, report = _json_run(["doctor"], wire, tmp_path / "fleet")
    assert code == 0, report
    assert all(entry["found"] for entry in report["data"]["tools"])


def test_doctor_fails_with_the_exact_command_when_a_tool_is_missing(tmp_path, monkeypatch):
    """"Install them" is the least useful thing to say at the moment somebody is stuck."""
    _no_tools(monkeypatch, present=("mpremote",))
    wire = FakeWire()
    code, report = _json_run(["doctor"], wire, tmp_path / "fleet")
    assert code == 1
    assert "esptool" in report["error"]
    plan = next(e for e in report["data"]["tools"] if e["tool"] == "esptool")["plan"]
    assert plan["argv"][1:4] == ["-m", "pip", "install"]
    assert plan["package"] == "esptool"
    assert plan["scripts_dir"]


def test_doctor_says_what_each_missing_tool_is_needed_for(tmp_path, monkeypatch, capsys):
    _no_tools(monkeypatch, present=())
    wire = FakeWire()
    main(["doctor", "--fleet", str(tmp_path / "fleet")], runner=wire,
         sleep=lambda _s: None)
    said = capsys.readouterr().out
    assert "ampy" in said                      # why mpremote is not optional
    assert "--firmware" in said                # why esptool is only sometimes needed
    assert "-m pip install" in said and "mpremote" in said


def test_doctor_install_runs_pip_and_then_looks_again(tmp_path, monkeypatch):
    """pip's exit code says the package is on the machine, not that the wizard can run it."""
    import shutil
    installed = set()
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: ("/opt/bin/" + name) if name in installed
                        else None)

    class _Pip(FakeWire):
        def run(self, argv, timeout=120, capture=True):
            self.calls.append(list(argv))
            if "-c" in argv:
                return 0, "/opt/bin\n/opt/userbin\n", ""
            if "pip" in argv and "install" in argv:
                installed.add({"mpremote": "mpremote", "esptool": "esptool.py"}[argv[-1]])
                return 0, "Successfully installed", ""
            return super().run(argv, timeout=timeout, capture=capture)

    wire = _Pip()
    code, report = _json_run(["doctor", "--install"], wire, tmp_path / "fleet")
    assert code == 0, report
    pips = [c for c in wire.calls if "pip" in c and "install" in c]
    assert [c[-1] for c in pips] == ["mpremote", "esptool"]
    assert all(entry["ok"] for entry in report["data"]["installed"])


def test_doctor_install_that_lands_off_path_is_not_called_a_success(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)

    class _PipToNowhere(FakeWire):
        def run(self, argv, timeout=120, capture=True):
            self.calls.append(list(argv))
            if "-c" in argv:
                return 0, "/not/writable\n/somewhere/off/path\n", ""
            if "pip" in argv and "install" in argv:
                return 0, "Successfully installed", ""
            return super().run(argv, timeout=timeout, capture=capture)

    code, report = _json_run(["doctor", "--install"], _PipToNowhere(), tmp_path / "fleet")
    assert code == 1
    assert "still missing" in report["error"]
    assert any("export PATH=" in entry["detail"] for entry in report["data"]["installed"])


def test_a_phase_does_not_start_without_the_tool_it_will_need(tmp_path, monkeypatch):
    """A board can be probed, backed up and half configured before a flash discovers that
    `esptool` was never there. The check belongs before the first step, not at the call."""
    _no_tools(monkeypatch, present=())
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 1
    assert "mpremote" in edge["error"]
    assert "provision doctor" in edge["error"]
    assert wire.calls == []                    # the board was never touched


def test_esptool_is_only_required_when_a_flash_was_asked_for(tmp_path, monkeypatch):
    _no_tools(monkeypatch, present=("mpremote",))
    wire = FakeWire()
    code, edge = _json_run(["edge", "--port", EDGE_PORT], wire, tmp_path / "fleet")
    assert code == 0, edge

    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x00")
    code, flashed = _json_run(["edge", "--port", EDGE_PORT, "--firmware", str(firmware)],
                              FakeWire(), tmp_path / "fleet2")
    assert code == 1
    assert "esptool" in flashed["error"]

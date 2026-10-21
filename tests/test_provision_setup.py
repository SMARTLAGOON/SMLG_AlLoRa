"""The guided path: one command, a conversation, a plan, and then a deployment.

The other commands are a toolkit and this is the layer on top of it. What that layer is judged
on is not smoothness -- it is whether the operator ever has to know something the machine
already knows. So the tests below pin the three things that make it more than a shell loop:

**Nothing is typed that the wizard can see for itself.** No port paths, no `device_id`, no
firmware path. Every answer in these scripts is either a menu number or an empty line taking a
default, and a run comes out with both ends provisioned and registered.

**The plan is shown before a board is touched**, and "no" leaves the desk exactly as it was.

**The human moments are the wizard's own.** The tap on RESET is a prompt inside the run, and
walking away from the keyboard is an answer it handles rather than a traceback.

The logic underneath is not re-tested here: `setup` drives `provision_edge`, `provision_hub`
and `verify_pair`, and if it ever grows its own copy of one of them, that is the bug.
"""
import io
import json
import shutil
import subprocess

import pytest

from tools.allora_provision.cli import main

EDGE_PORT = "/dev/cu.usbmodem1101"
HUB_PORT = "/dev/cu.usbmodem101"
THIRD_PORT = "/dev/cu.usbmodem2101"

MACS = {EDGE_PORT: "9eeff0dc", HUB_PORT: "9eeff0e0", THIRD_PORT: "9eeff0f4"}
DEVICE_IDS = {EDGE_PORT: "d909f4eb" + "11" * 28,
              HUB_PORT: "a1b2c3d4" + "22" * 28,
              THIRD_PORT: "beef0011" + "33" * 28}

_PASSING_HUB = ("VERIFY:mode secure\nVERIFY:aead yes\nVERIFY:endpoints 1\n"
                "VERIFY:session yes\nVERIFY:bytes 1000\nVERIFY:intact yes\n"
                "VERIFY:result pass\n")
_PASSING_EDGE = "VERIFY:mode secure\n"


class FakeWire:
    """Boards on the end of a pretend `mpremote`/`esptool`, told apart by their MAC."""

    def __init__(self, ports=(EDGE_PORT, HUB_PORT), posture="secure", unreadable=()):
        self.ports = list(ports)
        self.unreadable = set(unreadable)
        self.calls = []
        self.pushed = {}
        mode = "open" if posture == "open" else "secure"
        self.verify_output = {
            p: (_PASSING_HUB if p != self.ports[0] else _PASSING_EDGE).replace("secure", mode)
            for p in self.ports}

    def run(self, argv, timeout=120, capture=True):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        port = next((p for p in self.ports if p in joined), None)

        if argv[0].startswith("esptool"):
            return 0, "", ""
        if "ALLORA_PROBE" in joined:
            return 0, "ALLORA_PROBE\n", ""
        if "ALLORA_VERSION" in joined:
            return 0, "ALLORA_VERSION:3.0.0\n", ""
        if "network" in joined:
            return 0, "MAC:{}\n".format(
                "unknown" if port in self.unreadable else MACS[port]), ""
        if "load_or_create_identity" in joined:
            return 0, "DEVID:{}\n".format(DEVICE_IDS[port]), ""
        if "fs" in argv and "cat" in argv:
            return 1, "", "no such file"
        if "fs" in argv and "cp" in argv:
            local, remote = argv[-2], argv[-1].lstrip(":")
            with open(local, "rb") as f:
                raw = f.read()
            # Real `mpremote fs cp` copies bytes and does not care what is in them; a payload
            # queued for an Edge is not text. Decoded here only so the assertions below can go
            # on reading pushed files as strings.
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw
            self.pushed.setdefault(port, {})[remote] = content
            return 0, "", ""
        if "run" in argv:
            return 0, self.verify_output.get(port, ""), ""
        return 0, "", ""

    def on(self, port, name):
        return self.pushed.get(port, {}).get(name)

    def flashed(self):
        return [" ".join(a) for a in self.calls if "write_flash" in a]


def _answers(*script):
    """A keyboard that says exactly this and then complains about being asked more."""
    remaining = list(script)

    def read(_prompt=""):
        if not remaining:
            raise AssertionError("the wizard asked more questions than the script answers")
        return remaining.pop(0)
    return read


@pytest.fixture(autouse=True)
def a_bench(monkeypatch):
    """Two tools installed, no firmware images on disk, and only the ports a test names."""
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: ("/opt/bin/" + name)
                        if name in ("mpremote", "esptool.py") else None)
    monkeypatch.setattr("tools.allora_provision.setup.firmware_images", lambda *a, **k: [])


def _ports(monkeypatch, *ports):
    for module in ("board", "steps"):
        monkeypatch.setattr("tools.allora_provision.{}.candidate_ports".format(module),
                            lambda globs=None, _p=list(ports): list(_p))


def _run(script, fleet_dir, wire, argv=("setup",)):
    out = io.StringIO()
    code = main(list(argv) + ["--fleet", str(fleet_dir)], runner=wire, out=out,
                sleep=lambda _s: None, ask=_answers(*script))
    return code, out.getvalue()


def _registry(fleet_dir):
    with open(str(fleet_dir / "fleet.json")) as f:
        return json.load(f)


# The whole conversation on a two-board bench in the default posture, in order: posture, which
# board is the Edge, what that board is, what the Hub is, firmware, sf, freq, bandwidth,
# proceed. Every one taken as offered.
_ALL_DEFAULTS = ("", "", "", "", "", "", "", "", "")


def test_one_command_provisions_both_ends_and_proves_them(tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    roles = {e["role"]: e for e in _registry(tmp_path / "fleet")}
    assert set(roles) == {"edge", "hub"}
    assert roles["edge"]["device_id"] == DEVICE_IDS[EDGE_PORT]
    assert wire.on(EDGE_PORT, "main.py") and wire.on(HUB_PORT, "Nodes.json")
    # The Hub polls the Edge the wizard registered, and nobody copied the fingerprint across.
    assert json.loads(wire.on(HUB_PORT, "Nodes.json"))[0]["device_id"] == DEVICE_IDS[EDGE_PORT]


def test_the_boards_are_offered_by_the_mac_that_says_which_is_which(tmp_path, monkeypatch,
                                                                    capsys):
    """The MAC is the identity; the port is not, and it changes on every hard reset."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    _run(_ALL_DEFAULTS, tmp_path / "fleet", FakeWire())
    asked = capsys.readouterr().out
    assert MACS[EDGE_PORT] in asked and MACS[HUB_PORT] in asked
    assert "Which board is the Edge?" in asked


def test_the_plan_is_shown_first_and_no_leaves_the_desk_alone(tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    code, tail = _run(_ALL_DEFAULTS[:-1] + ("n",), tmp_path / "fleet", wire)

    shown = capsys.readouterr().out
    assert "Plan" in shown and "one real 1000-byte transfer" in shown
    assert code == 1
    assert wire.pushed == {}, "a board was touched before the plan was approved"
    assert "nothing was changed" in tail


def test_a_single_board_is_half_a_deployment_and_says_so(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT)
    code, tail = _run((), tmp_path / "fleet", FakeWire(ports=(EDGE_PORT,)))
    assert code == 1
    assert "needs two" in tail


def test_the_open_posture_addresses_each_edge_by_its_mac_and_asks_no_number(
        tmp_path, monkeypatch, capsys):
    """An open node takes its sid from the low byte of its MAC, and the Hub derives the same one
    from the MAC in its roster. A number typed by hand could only disagree with that."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire(posture="open")
    # posture=1 (open), edge, its hardware, the Hub's, firmware, sf, freq, bandwidth, proceed
    code, tail = _run(("1",) + ("",) * 8, tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    assert "session_id" not in json.loads(wire.on(EDGE_PORT, "AlLoRa.json"))
    assert "session_id" not in json.loads(wire.on(HUB_PORT, "AlLoRa.json"))
    roster = json.loads(wire.on(HUB_PORT, "Nodes.json"))
    assert [row["mac_address"] for row in roster] == [MACS[EDGE_PORT]]
    assert "session_id" not in roster[0]


def test_an_open_deployment_takes_several_edges_like_any_other(tmp_path, monkeypatch):
    """Each Edge has a MAC of its own, so each has an address of its own."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT), posture="open")
    # posture=open, 2 edges, edge 1, edge 2 (the third board, so the Hub is the one left),
    # each one's hardware, the Hub's, firmware, sf, freq, bandwidth, proceed
    code, tail = _run(("1", "2", "", "2") + ("",) * 8, tmp_path / "fleet", wire)

    assert code == 0, tail
    roster = json.loads(wire.on(HUB_PORT, "Nodes.json"))
    assert sorted(row["mac_address"] for row in roster) == sorted(
        [MACS[EDGE_PORT], MACS[THIRD_PORT]])


def test_flashing_is_offered_and_never_taken_by_pressing_enter(tmp_path, monkeypatch, capsys):
    """A flash erases the board and its identity with it. Enter all the way keeps both."""
    _images(monkeypatch, _image(tmp_path, "t3s3-sx127x"))
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    assert wire.flashed() == []


def test_choosing_to_flash_says_the_board_comes_back_as_a_new_node(tmp_path, monkeypatch,
                                                                   capsys):
    _images(monkeypatch, _image(tmp_path, "t3s3-sx127x"))
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    _run(("", "", "", "", "1", "", "", "", "n"), tmp_path / "fleet", FakeWire())
    assert "new device_id" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_fleet_git_would_commit_is_refused_before_the_first_question(tmp_path, monkeypatch,
                                                                      capsys):
    """Refused at the end, the operator loses every answer they gave to get there."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    code, tail = _run((), tmp_path / "fleet", FakeWire())

    assert code == 1
    assert "would commit" in capsys.readouterr().out + tail


def test_a_board_whose_mac_cannot_be_read_is_refused_before_the_questions(tmp_path,
                                                                         monkeypatch):
    """A board is named by its MAC in the plan, so one without a MAC cannot be in a plan."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    code, tail = _run((), tmp_path / "fleet", FakeWire(unreadable=(HUB_PORT,)))

    assert code == 1
    assert "could not read the MAC" in tail
    assert HUB_PORT in tail


def test_a_third_board_with_no_mac_is_left_out_and_the_pair_goes_ahead(tmp_path, monkeypatch,
                                                                       capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT), unreadable=(THIRD_PORT,))
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    assert wire.pushed.get(THIRD_PORT) is None


def test_a_second_edge_is_registered_under_a_name_of_its_own(tmp_path, monkeypatch):
    """Two Edges that share a name are one entry in the roster, and the Hub polls one node."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT))
    # posture, 2 edges, edge 1, edge 2, each one's hardware, the Hub's, firmware, sf, freq,
    # bandwidth, proceed
    code, tail = _run(("", "2") + ("",) * 11, tmp_path / "fleet", wire)

    assert code == 0, tail
    edges = [e for e in _registry(tmp_path / "fleet") if e["role"] == "edge"]
    assert sorted(e["name"] for e in edges) == ["S1", "S2"]
    assert len({e["device_id"] for e in edges}) == 2


def _images(monkeypatch, *paths):
    monkeypatch.setattr("tools.allora_provision.setup.firmware_images",
                        lambda *a, **k: [str(p) for p in paths])


def _image(tmp_path, target):
    path = tmp_path / "AlLoRa-{}-firmware.bin".format(target)
    path.write_bytes(b"\x00")
    return path


def test_the_build_for_each_board_target_is_the_one_written(tmp_path, monkeypatch, capsys):
    image = _image(tmp_path, "t3s3-sx127x")
    _images(monkeypatch, image)
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    # posture, edge, its hardware, the Hub's, firmware=1 (flash, not keep), sf, freq, bw, go
    code, tail = _run(("", "", "", "", "1", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    assert len(wire.flashed()) == 2
    assert all(str(image) in call for call in wire.flashed())


def test_a_mixed_pair_is_provisioned_with_each_end_on_its_own_radio(tmp_path, monkeypatch,
                                                                    capsys):
    """The bug this whole shape exists for. The bench pair is an SX1262 Edge and an SX127x Hub.

    One radio for the run wrote the same `driver` into both boards, so one of them was
    provisioned for a chip it does not have. The board halts at boot on a driver it cannot
    build, which is the loud half; the quiet half is that the wizard could not describe the
    deployment that took a session to get working, and never said so.
    """
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    # The Edge is the E-Paper board with an SX1262 (option 4), the Hub a plain T3-S3 with an
    # SX127x (option 1).
    code, tail = _run(("", "", "4", "1", "", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    edge = json.loads(wire.on(EDGE_PORT, "AlLoRa.json"))
    hub = json.loads(wire.on(HUB_PORT, "AlLoRa.json"))
    assert edge["connector"]["driver"] == "sx1262"
    assert hub["connector"]["driver"] == "sx127x"
    # The pins are the half that fails silently: an SX1262 with no map falls back to another
    # board's, and the run reports success on a board that never finds its chip.
    assert edge["connector"]["clk"] == 5
    assert "clk" not in hub["connector"], "sx127x reads its pins from its own driver"

    registry = {e["role"]: e for e in _registry(tmp_path / "fleet")}
    assert registry["edge"]["radio"] == "sx1262"
    assert registry["edge"]["board"] == "t3s3-epaper", "the name the operator gave, not the row"
    assert registry["hub"]["radio"] == "sx127x"


def test_the_proof_on_a_mixed_pair_does_not_warn_that_the_hub_has_the_wrong_radio(
        tmp_path, monkeypatch, capsys):
    """Found on the bench, 2026-10-20: an SX1262 Edge and an SX127x Hub, transfer passed.

    The proof was handed the Edge's radio as what both ends were expected to be, so it warned
    that the Hub "was told sx1262". The warning is meant for boards that differ from their
    record, and on a mixed pair it fired every time for a Hub that was exactly as recorded.
    """
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    code, tail = _run(("", "", "4", "1", "", "", "", "", ""), tmp_path / "fleet", wire)

    said = capsys.readouterr().out + tail
    assert code == 0, said
    assert "registered as" not in said, said


def test_every_board_is_left_running_its_program_when_setup_ends(tmp_path, monkeypatch, capsys):
    """Found on the bench, 2026-10-20: after "Done", both boards sat at the REPL prompt.

    A soft reset issued through `mpremote exec` comes back to the REPL without running
    `main.py`, and the proof runs under `mpremote run`, which leaves the board at the prompt
    too. A hard reset is the one that boots into the program, so it is the last thing each
    board is sent.
    """
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    for port in (EDGE_PORT, HUB_PORT):
        last = [c for c in wire.calls if port in c][-1]
        assert last[-1] == "reset", (port, last)


def test_the_last_lines_say_where_to_put_a_file_and_where_it_lands(tmp_path, monkeypatch,
                                                                  capsys):
    """Benja's first run, 2026-10-19: it worked, and then it pointed at the fleet record and
    said nothing about what to do with a running pair. The Hub files what arrives under the
    Edge's label, not its name, so that is the folder the line has to give."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", FakeWire())

    shown = capsys.readouterr().out + tail
    assert code == 0, shown
    assert "mpremote connect {} fs cp".format(EDGE_PORT) in shown
    assert ":Outbox/" in shown
    assert "Results/{}/".format(DEVICE_IDS[EDGE_PORT][:8]) in shown
    assert "mpremote connect {} repl".format(HUB_PORT) in shown


def test_the_folder_comes_from_the_newest_record_of_a_reflashed_board():
    # A board reflashed into a new slot keeps its MAC and gets a new identity, and the old
    # record stays in the registry ahead of the new one. The Hub files under the new label.
    from tools.allora_provision.setup import Prompt, _next_steps
    old, new = "aaaa0000" + "00" * 28, "bbbb1111" + "11" * 28
    entries = [{"role": "edge", "name": "S", "mac": "9eeff0dc", "device_id": old},
               {"role": "hub", "name": "R", "mac": "9eeff0e0", "device_id": "cc" * 32},
               {"role": "edge", "name": "S2", "mac": "9eeff0dc", "device_id": new}]
    plan = {"edges": [{"mac": "9eeff0dc", "name": "S2"}], "hub": {"mac": "9eeff0e0"}}
    boards = [{"mac": "9eeff0dc", "port": EDGE_PORT}, {"mac": "9eeff0e0", "port": HUB_PORT}]
    out = io.StringIO()

    _next_steps(Prompt(out=out), plan, entries, boards)

    assert "Results/bbbb1111/" in out.getvalue(), out.getvalue()


def test_every_command_the_wizard_prints_runs_as_printed():
    # There is no installed `provision` command: the tool is tools/provision.py.
    import inspect
    from tools.allora_provision import setup as setup_module
    code = [line for line in inspect.getsource(setup_module).split('"""', 2)[2].splitlines()
            if not line.lstrip().startswith("#")]
    assert not [line for line in code if "`provision " in line]


def test_an_open_edge_s_files_land_under_its_mac(tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    code, tail = _run(("1",) + ("",) * 8, tmp_path / "fleet", FakeWire(posture="open"))

    shown = capsys.readouterr().out + tail
    assert code == 0, shown
    assert "Results/{}/".format(MACS[EDGE_PORT]) in shown


class _ResetHangs(FakeWire):
    """A board whose `mpremote reset` runs out its timeout, as when a monitor holds the port."""

    def __init__(self, hanging, **kwargs):
        FakeWire.__init__(self, **kwargs)
        self.hanging = hanging

    def run(self, argv, timeout=120, capture=True):
        if self.hanging in argv and argv[-1] == "reset":
            self.calls.append(list(argv))
            from tools.allora_provision.board import BoardError
            raise BoardError("mpremote did not finish within {}s".format(timeout))
        return FakeWire.run(self, argv, timeout=timeout, capture=capture)


def test_one_board_that_will_not_reset_does_not_keep_the_others_from_starting(
        tmp_path, monkeypatch, capsys):
    """Every phase finished. A slow reset on the first board is a tap on RESET for that board,
    not a failed run and a Hub left at the prompt."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = _ResetHangs(EDGE_PORT)
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    said = capsys.readouterr().out + tail
    assert code == 0, said
    assert [c for c in wire.calls if HUB_PORT in c][-1][-1] == "reset"
    assert "Tap RESET" in said


class _WriteFails(FakeWire):
    """A board that refuses to take a file, which ends the phase that was writing to it."""

    def __init__(self, failing, **kwargs):
        FakeWire.__init__(self, **kwargs)
        self.failing = failing

    def run(self, argv, timeout=120, capture=True):
        if self.failing in argv and "cp" in argv:
            self.calls.append(list(argv))
            return 1, "", "OSError: 28"
        return FakeWire.run(self, argv, timeout=timeout, capture=capture)


def test_a_run_that_stops_halfway_still_starts_the_boards_it_finished(tmp_path, monkeypatch):
    """Edge 1 is provisioned and Edge 2 fails. Edge 1 is a finished node, and it was left at the
    prompt because the reset only ran at the end of a run that succeeded."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = _WriteFails(THIRD_PORT, ports=(EDGE_PORT, HUB_PORT, THIRD_PORT))
    # posture, 2 edges, edge 1, edge 2 (the third board), each one's hardware, the Hub's,
    # firmware, sf, freq, bandwidth, proceed
    code, tail = _run(("", "2", "", "2") + ("",) * 8, tmp_path / "fleet", wire)

    assert code == 1, tail
    assert [c for c in wire.calls if EDGE_PORT in c][-1][-1] == "reset"
    # The run never reached the Hub, so it holds exactly what it ran before discovery read its
    # MAC and stopped it. Left at the prompt, a fleet's running Hub would be off the air.
    assert [c for c in wire.calls if HUB_PORT in c][-1][-1] == "reset", \
        "a board the run never reached was left stopped"
    # Edge 2's own phase failed partway, so it is not started into half its files.
    assert all(c[-1] != "reset" for c in wire.calls if THIRD_PORT in c)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_new_deployment_folder_git_would_commit_is_refused_before_the_questions(
        tmp_path, monkeypatch, capsys):
    """The first folder was checked before the first question; the one named for a new
    deployment was checked only after "Proceed?", and every answer was lost."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("fleet/\n")
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "fleet.json").write_text(json.dumps(
        [{"name": "S", "role": "edge", "posture": "secure", "device_id": "aa" * 32}]))
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    # a new deployment, in the folder offered
    code, tail = _run(("2", ""), tmp_path / "fleet", FakeWire())

    assert code == 1, tail
    assert "would commit" in capsys.readouterr().out + tail


def test_a_board_with_no_build_for_its_target_keeps_what_it_runs(tmp_path, monkeypatch, capsys):
    """The dangerous half of a mixed pair, and the reason firmware is per node.

    One image for the run would have flashed the SX1262 board with the SX127x build, and every
    step of that run reports success: the flash writes, the config is pushed, the node boots
    into a program whose radio is not there.
    """
    image = _image(tmp_path, "t3s3-sx127x")
    _images(monkeypatch, image)
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    # An SX1262 Edge, an SX127x Hub, and flash.
    code, tail = _run(("", "", "4", "1", "1", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    flashed = wire.flashed()
    assert len(flashed) == 1, "only the board whose target has a build is flashed"
    assert HUB_PORT in flashed[0]
    said = capsys.readouterr().out
    assert "no t3s3-sx1262 build on this machine" in said
    assert "keeps what it runs" in said


def test_a_run_stopped_at_the_first_question_leaves_every_board_running(
        tmp_path, monkeypatch, capsys):
    """Found on the bench, 2026-10-21: reading each board's MAC stops its program, and a run
    stopped at the first question left all three boards at the prompt, off the air."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT))

    def gone(_prompt=""):
        raise EOFError      # `printf '' | provision.py setup`, as on the bench

    out = io.StringIO()
    code = main(["setup", "--fleet", str(tmp_path / "fleet")], runner=wire, out=out,
                sleep=lambda _s: None, ask=gone)

    assert code == 1, out.getvalue()
    assert "Nothing was changed on any board" in out.getvalue()
    for port in (EDGE_PORT, HUB_PORT, THIRD_PORT):
        assert [c for c in wire.calls if port in c][-1][-1] == "reset", port


def test_a_board_left_out_of_the_plan_is_left_running(tmp_path, monkeypatch, capsys):
    """The full run on two of three boards: the third was only asked its MAC, and it stayed
    at the prompt while the other two were restarted."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT))
    # posture, one Edge, the first board, its hardware, the Hub (the second board), its
    # hardware, firmware, sf, freq, bandwidth, proceed
    code, tail = _run(("", "", "", "", "", "", "", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, capsys.readouterr().out + tail
    assert [c for c in wire.calls if THIRD_PORT in c][-1][-1] == "reset"


def test_ports_leaves_every_board_it_read_running(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    out = io.StringIO()
    code = main(["ports"], runner=wire, out=out, sleep=lambda _s: None)

    assert code == 0, out.getvalue()
    for port in (EDGE_PORT, HUB_PORT):
        assert [c for c in wire.calls if port in c][-1][-1] == "reset", port


def test_walking_away_from_the_keyboard_is_an_answer_and_not_a_traceback(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)

    def gone(_prompt=""):
        raise EOFError

    out = io.StringIO()
    code = main(["setup", "--fleet", str(tmp_path / "fleet")], runner=FakeWire(), out=out,
                sleep=lambda _s: None, ask=gone)
    assert code == 1
    assert "Nothing was changed on any board" in out.getvalue()


def test_setup_holds_a_conversation_and_so_takes_no_json(tmp_path):
    """`--json` is the website's surface, on the commands it drives. This one is additive."""
    with pytest.raises(SystemExit) as raised:
        main(["setup", "--json", "--fleet", str(tmp_path / "fleet")])
    assert raised.value.code == 2


# --- the tools the wizard shells out to --------------------------------------------------

def _tools(monkeypatch, present):
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: ("/opt/bin/" + name) if name in present else None)


def test_a_machine_missing_a_tool_is_offered_the_install_rather_than_a_lecture(
        tmp_path, monkeypatch, capsys):
    """"Install them" is the least useful thing to say to somebody who is already stuck."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    installed = []

    class Pip(FakeWire):
        def run(self, argv, timeout=120, capture=True):
            if "pip" in argv:
                installed.append(list(argv))
                _tools(monkeypatch, ("mpremote", "esptool.py"))   # now it is there
                return 0, "Successfully installed mpremote", ""
            return FakeWire.run(self, argv, timeout=timeout, capture=capture)

    _tools(monkeypatch, ())
    wire = Pip()
    code, tail = _run(("y",) + _ALL_DEFAULTS, tmp_path / "fleet", wire)

    offered = capsys.readouterr().out
    assert "mpremote is needed for" in offered
    assert "install with: " in offered
    assert installed, "the wizard said it would install and did not"
    assert code == 0, tail


def test_declining_the_install_gives_the_command_rather_than_a_dead_end(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    _tools(monkeypatch, ())
    code, tail = _run(("n",), tmp_path / "fleet", FakeWire())
    assert code == 1
    assert "python3 tools/provision.py doctor --install" in tail


class _OldLibraryWire(FakeWire):
    """A board whose firmware froze the library before 3.0 moved it into its folders."""

    def __init__(self, old_port, **kwargs):
        FakeWire.__init__(self, **kwargs)
        self.old_port = old_port

    def run(self, argv, timeout=120, capture=True):
        if self.old_port in argv and "ALLORA_VERSION" in " ".join(argv):
            self.calls.append(list(argv))
            return 1, "", "ImportError: no module named 'AlLoRa.Data'"
        return FakeWire.run(self, argv, timeout=timeout, capture=capture)


@pytest.mark.parametrize("old_port", [EDGE_PORT, HUB_PORT])
def test_a_board_on_firmware_older_than_the_program_gets_nothing_written(
        tmp_path, monkeypatch, old_port):
    """The program imports the 3.0 folders, and the library is frozen into the firmware. Pushed
    onto an older build it stops at boot with an ImportError, after every step said ok."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = _OldLibraryWire(old_port)
    code, tail = _run(_ALL_DEFAULTS, tmp_path / "fleet", wire)

    assert code != 0
    assert "firmware" in tail and "3.0" in tail
    assert wire.on(old_port, "main.py") is None
    assert wire.on(old_port, "AlLoRa.json") is None

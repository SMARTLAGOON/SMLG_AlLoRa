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

    def __init__(self, ports=(EDGE_PORT, HUB_PORT), posture="secure"):
        self.ports = list(ports)
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
        if "network" in joined:
            return 0, "MAC:{}\n".format(MACS[port]), ""
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


def test_the_open_posture_asks_for_the_number_both_ends_carry(tmp_path, monkeypatch):
    """Open v3 is sid-addressed and there is no identity to derive an address from."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire(posture="open")
    # posture=1 (open), edge, its hardware, the Hub's, session id 7, firmware, sf, freq,
    # bandwidth, proceed
    code, tail = _run(("1", "", "", "", "7", "", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, tail
    assert json.loads(wire.on(EDGE_PORT, "AlLoRa.json"))["session_id"] == 7
    assert json.loads(wire.on(HUB_PORT, "AlLoRa.json"))["session_id"] == 7


def test_open_addressing_provisions_one_pair_and_says_why(tmp_path, monkeypatch, capsys):
    """A silent cap would read as "it covered everything". This one is stated out loud."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(EDGE_PORT, HUB_PORT, THIRD_PORT), posture="open")
    # No "how many edges" question is asked at all in this posture.
    code, tail = _run(("1", "", "", "", "", "1", "", "", "", "", ""), tmp_path / "fleet", wire)

    assert code == 0, tail
    said = capsys.readouterr().out
    assert "one number shared by one pair" in said
    assert len([e for e in _registry(tmp_path / "fleet") if e["role"] == "edge"]) == 1


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
    assert "provision doctor --install" in tail

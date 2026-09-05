"""The plan file: one document, two front doors, and a deployment that survives being extended.

`setup` and `apply` are the same engine reached two ways. What the plan file buys is that the
run stops being a conversation nobody can repeat: the interactive path writes down what it was
told to do, and something that is not a person at a terminal can write the same document and
hand it to `apply`.

Three things are pinned here, and each one is a way a deployment used to break quietly.

**A plan names boards by MAC.** The port re-enumerates on every hard reset, so a plan that
recorded ports would aim yesterday's intent at whichever board happens to be on that path
today.

**Extending is a mode, not an accident.** A run that walks into a live deployment without
knowing it is there orphans a record and appends a phantom, and the Hub then spends a listening
window every cycle polling a node nobody holds.

**A roster row this run did not provision is not rewritten.** `Nodes.json` is a read/write
state file: the Hub writes an endpoint's settled radio settings back into it when a retune is
accepted. Rebuilding that file from the operator's registry puts the Hub back on settings its
Edge left months ago, and nothing on either side says so.
"""
import io
import json

import pytest

from tools.allora_provision import plan as plan_module
from tools.allora_provision.cli import main
from tools.allora_provision.fleet import Fleet

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
    """Boards with a filesystem, so a run can be made to walk into one that already has files.

    The wire in `test_provision_setup` answers "no such file" to every read, which is a board
    fresh out of the box. Everything this module is about happens on the second run, so here a
    read comes back with whatever a previous run -- or the Hub itself -- left behind.
    """

    def __init__(self, ports=(EDGE_PORT, HUB_PORT), edge_port=None, posture="secure"):
        self.ports = list(ports)
        self.calls = []
        self.files = {port: {} for port in self.ports}
        edge_port = edge_port or self.ports[0]
        mode = "open" if posture == "open" else "secure"
        self.verify_output = {
            p: (_PASSING_EDGE if p == edge_port else _PASSING_HUB).replace("secure", mode)
            for p in self.ports}

    def place(self, port, name, content):
        """Put a file on a board the way a previous run or the Hub's own writes would."""
        self.files.setdefault(port, {})[name] = content
        return self

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
        if "fs" in argv:
            return self._filesystem(argv, port)
        if "run" in argv:
            return 0, self.verify_output.get(port, ""), ""
        return 0, "", ""

    def _filesystem(self, argv, port):
        """The board's filesystem: what a previous run, or the Hub itself, left on it."""
        if "cat" in argv:
            held = self.files.get(port, {}).get(argv[-1].lstrip(":"))
            return (1, "", "no such file") if held is None else (0, held, "")
        if "rm" in argv:
            self.files.get(port, {}).pop(argv[-1].lstrip(":"), None)
            return 0, "", ""
        if "cp" in argv:
            local, remote = argv[-2], argv[-1].lstrip(":")
            with open(local, "rb") as f:
                raw = f.read()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw
            self.files.setdefault(port, {})[remote] = content
        return 0, "", ""

    def on(self, port, name):
        return self.files.get(port, {}).get(name)

    def flashed(self):
        return [" ".join(a) for a in self.calls if "write_flash" in a]


def _answers(*script):
    remaining = list(script)

    def read(_prompt=""):
        if not remaining:
            raise AssertionError("the wizard asked more questions than the script answers")
        return remaining.pop(0)
    return read


@pytest.fixture(autouse=True)
def a_bench(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: ("/opt/bin/" + name)
                        if name in ("mpremote", "esptool.py") else None)
    monkeypatch.setattr("tools.allora_provision.setup.firmware_images", lambda *a, **k: [])


def _ports(monkeypatch, *ports):
    for module in ("board", "steps"):
        monkeypatch.setattr("tools.allora_provision.{}.candidate_ports".format(module),
                            lambda globs=None, _p=list(ports): list(_p))


def _run(script, wire, argv):
    out = io.StringIO()
    code = main(list(argv), runner=wire, out=out, sleep=lambda _s: None,
                ask=_answers(*script))
    return code, out.getvalue()


def _setup(script, fleet_dir, wire):
    return _run(script, wire, ["setup", "--fleet", str(fleet_dir)])


def _registry(fleet_dir):
    with open(str(fleet_dir / "fleet.json")) as f:
        return json.load(f)


def _plan(fleet_dir):
    with open(str(fleet_dir / "plan.json")) as f:
        return json.load(f)


# A first run on an empty fleet, in order: posture, which board is the Edge, what that board
# is, what the Hub is, firmware, sf, freq, bandwidth, proceed. Every answer taken as offered.
_FIRST_RUN = ("", "", "", "", "", "", "", "", "")

# A second run over that fleet, adding the third board. The same questions with the
# scratch-or-extend one in front: extend, posture, how many Edges are added, which board (the
# third, which is the one not already in the fleet), new-or-replacement, what that board is,
# which board is the Hub, firmware, sf, freq, bandwidth, proceed. The Hub is not asked what it
# is: an extend run writes one row in its roster and never its config.
_EXTEND_RUN = ("", "", "", "3", "", "", "", "", "", "", "", "")


def _node(**fields):
    """One node entry as a plan holds it, with the bench pair's hardware unless told otherwise."""
    entry = {"mac": None, "name": None, "board": "t3s3", "radio": "sx127x", "firmware": None}
    entry.update(fields)
    return entry


# --- the document ---------------------------------------------------------------------------

def test_a_plan_names_boards_by_mac_because_a_port_is_not_an_identity(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    code, tail = _setup(_FIRST_RUN, tmp_path / "fleet", FakeWire())

    assert code == 0, tail
    written = _plan(tmp_path / "fleet")
    assert written["hub"]["mac"] == MACS[HUB_PORT]
    assert [e["mac"] for e in written["edges"]] == [MACS[EDGE_PORT]]
    # The thing that would rot. A port belongs to a run, not to a board.
    assert EDGE_PORT not in json.dumps(written)
    assert HUB_PORT not in json.dumps(written)


def test_the_plan_is_written_before_a_board_is_touched(tmp_path, monkeypatch):
    """A run that dies halfway still leaves the intent behind for `apply` to pick up."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    wire = FakeWire()
    seen = {}
    import tools.allora_provision.setup as setup_module
    real = setup_module.provision_edge

    def watched(board, fleet, result, **kwargs):
        seen["plan_on_disk"] = (tmp_path / "fleet" / "plan.json").exists()
        return real(board, fleet, result, **kwargs)
    monkeypatch.setattr(setup_module, "provision_edge", watched)

    _setup(_FIRST_RUN, tmp_path / "fleet", wire)
    assert seen["plan_on_disk"] is True


def _plan_for(edges, hub, **fields):
    document = dict(mode="scratch", fleet="f", posture="secure",
                    rf={"sf": 7, "freq": 868, "bandwidth": 125})
    document.update(fields)
    return plan_module.build(edges=edges, hub=hub, **document)


def test_a_plan_from_a_future_toolkit_is_refused_rather_than_guessed_at():
    with pytest.raises(ValueError) as raised:
        plan_module.validate({"version": 99, "mode": "scratch", "fleet": "f",
                              "posture": "secure", "rf": {},
                              "edges": [_node(mac="aa")], "hub": _node(mac="bb")})
    assert "version 99" in str(raised.value)


def test_one_board_cannot_be_both_ends_of_its_own_deployment():
    with pytest.raises(ValueError) as raised:
        plan_module.validate(_plan_for([_node(mac="9eeff0dc", name="S")],
                                       _node(mac="9eeff0dc")))
    assert "named twice" in str(raised.value)


def test_two_edges_under_one_name_are_refused_because_a_name_is_a_slot():
    with pytest.raises(ValueError) as raised:
        plan_module.validate(_plan_for(
            [_node(mac="aa", name="S2"), _node(mac="bb", name="S2")], _node(mac="cc")))
    assert "one name is one node" in str(raised.value)


def test_a_plan_naming_a_board_nobody_plugged_in_fails_before_anything_runs():
    doc = _plan_for([_node(mac="9eeff0dc")], _node(mac="9eeff0e0"))
    with pytest.raises(ValueError) as raised:
        plan_module.ports_for(doc, [{"port": EDGE_PORT, "mac": "9eeff0dc"}])
    message = str(raised.value)
    assert "9eeff0e0" in message and "1 board is" in message


# --- what a node is made of -------------------------------------------------------------------

def test_each_node_carries_its_own_board_and_radio_so_a_mixed_pair_is_expressible():
    """The bench pair, as a document. One radio for the run could not say this."""
    doc = _plan_for([_node(mac="4a274ae0", board="t3s3-epaper", radio="sx1262")],
                    _node(mac="9eeff0e0", board="t3s3", radio="sx127x"))
    plan_module.validate(doc)
    assert doc["edges"][0]["radio"] == "sx1262"
    assert doc["hub"]["radio"] == "sx127x"
    assert "radio" not in doc, "a run-level radio is the thing that could not describe this"


def test_the_rf_settings_stay_run_level_because_both_ends_have_to_agree():
    doc = _plan_for([_node(mac="aa", radio="sx1262")], _node(mac="bb"))
    assert doc["rf"] == {"sf": 7, "freq": 868, "bandwidth": 125}
    for entry in [doc["hub"]] + doc["edges"]:
        assert not set(entry) & {"sf", "freq", "bandwidth"}, \
            "a per-node tuning is a way to build a fleet that cannot talk"


def test_a_node_that_names_no_radio_describes_half_a_node():
    with pytest.raises(ValueError) as raised:
        plan_module.validate(_plan_for([_node(mac="aa", radio=None)], _node(mac="bb")))
    assert "names no radio" in str(raised.value)


def test_a_board_that_cannot_carry_the_radio_is_refused_permanently():
    """Not a deployment waiting on a pin map: a transceiver is part of a board's design."""
    with pytest.raises(ValueError) as raised:
        plan_module.validate(_plan_for([_node(mac="aa", radio="lopy4")], _node(mac="bb")))
    assert "does not carry" in str(raised.value)


def test_a_pair_nobody_has_recorded_the_pins_for_is_refused_until_somebody_does():
    """The other refusal, and it says something different: this one is real and unmeasured."""
    with pytest.raises(ValueError) as raised:
        plan_module.validate(_plan_for([_node(mac="aa", radio="e5")], _node(mac="bb")))
    message = str(raised.value)
    assert "is a real combination" in message
    assert "add them to the board table" in message


# --- a plan written before the hardware is on the desk ------------------------------------------

def test_a_plan_may_describe_a_deployment_no_board_has_been_assigned_to():
    """Board, radio, role and posture are design-time facts. Which unit fills the slot is not."""
    doc = _plan_for([_node(name="S", board="t3s3", radio="sx1262")], _node())
    plan_module.validate(doc)
    assert plan_module.unbound(doc) == ["the hub", "S"]


def test_an_unbound_plan_is_refused_at_the_moment_something_would_be_flashed():
    doc = _plan_for([_node(name="S")], _node(mac="9eeff0e0"))
    with pytest.raises(ValueError) as raised:
        plan_module.require_bound(doc)
    message = str(raised.value)
    assert "no board is assigned to yet (S)" in message
    assert "provision setup" in message


# --- the second front door ------------------------------------------------------------------

def test_apply_runs_the_plan_setup_wrote_and_asks_nothing(tmp_path, monkeypatch):
    """The whole point: the same deployment, from the document, with no keyboard at all."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    first = tmp_path / "fleet"
    code, tail = _setup(_FIRST_RUN, first, FakeWire())
    assert code == 0, tail

    second = tmp_path / "again"
    wire = FakeWire()
    code, tail = _run((), wire, ["apply", str(first / "plan.json"), "--fleet", str(second)])

    assert code == 0, tail
    roles = {e["role"]: e for e in _registry(second)}
    assert set(roles) == {"edge", "hub"}
    assert roles["edge"]["device_id"] == DEVICE_IDS[EDGE_PORT]
    assert json.loads(wire.on(HUB_PORT, "Nodes.json"))[0]["device_id"] == DEVICE_IDS[EDGE_PORT]


def test_apply_says_which_board_is_missing_rather_than_provisioning_half_a_pair(
        tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    first = tmp_path / "fleet"
    _setup(_FIRST_RUN, first, FakeWire())

    # The Hub has been unplugged since the plan was written.
    _ports(monkeypatch, EDGE_PORT)
    wire = FakeWire(ports=(EDGE_PORT,))
    code, tail = _run((), wire, ["apply", str(first / "plan.json"),
                                 "--fleet", str(tmp_path / "again")])

    assert code != 0
    assert MACS[HUB_PORT] in tail
    assert wire.on(EDGE_PORT, "AlLoRa.json") is None, "no board should have been touched"


def test_apply_carries_the_run_back_as_json(tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    first = tmp_path / "fleet"
    _setup(_FIRST_RUN, first, FakeWire())
    capsys.readouterr()

    code = main(["apply", str(first / "plan.json"), "--fleet", str(tmp_path / "again"),
                 "--json"], runner=FakeWire(), sleep=lambda _s: None)
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["command"] == "apply"
    assert document["ok"] is True
    assert document["data"]["plan"]["hub"]["mac"] == MACS[HUB_PORT]


# --- extending a deployment that is already in the field -------------------------------------

def test_a_second_run_is_told_what_is_already_there_and_offered_extend(
        tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())
    capsys.readouterr()

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir))
    _setup(_EXTEND_RUN, fleet_dir, wire)

    asked = capsys.readouterr().out
    assert "This fleet already holds 2 node(s)" in asked
    assert "extend it" in asked
    assert "How many Edge nodes are you adding?" in asked


def _hub_roster(fleet_dir, **settled):
    """The roster the Hub holds: what the wizard wrote, plus whatever the field settled on."""
    roster = json.loads(Fleet(str(fleet_dir)).render_nodes_json())
    for entry in roster:
        entry.setdefault("connector", {}).update(settled)
    return json.dumps(roster, indent=2) + "\n"


def test_extending_leaves_a_settled_row_exactly_as_the_hub_has_it(tmp_path, monkeypatch):
    """The load-bearing one. The Hub's copy is what its Edges are actually listening on."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    code, tail = _setup(_FIRST_RUN, fleet_dir, FakeWire())
    assert code == 0, tail

    # S accepted a retune in the field and the Hub wrote it down, so the Hub says sf9 where the
    # operator's registry still says sf7.
    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir, sf=9, freq=869))
    code, tail = _setup(_EXTEND_RUN, fleet_dir, wire)
    assert code == 0, tail

    roster = {e["name"]: e for e in json.loads(wire.on(HUB_PORT, "Nodes.json"))}
    assert set(roster) == {"S", "S2"}, "the new Edge is added, the old one is not duplicated"
    assert roster["S"]["connector"]["sf"] == 9, "the settled value was overwritten"
    assert roster["S"]["connector"]["freq"] == 869
    assert roster["S2"]["connector"]["sf"] == 7, "the new Edge is on what the plan gave it"
    assert roster["S2"]["device_id"] == DEVICE_IDS[THIRD_PORT]


def test_extending_says_which_rows_it_kept_and_why(tmp_path, monkeypatch, capsys):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir, sf=9))
    capsys.readouterr()
    _setup(_EXTEND_RUN, fleet_dir, wire)

    said = capsys.readouterr().out
    assert "S on sf 9" in said
    assert "accepts a retune" in said


def test_extending_does_not_reflash_the_hub(tmp_path, monkeypatch):
    """One row in one file changed, so the Hub keeps its config, its program and its identity."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir))
    wire.place(HUB_PORT, "AlLoRa.json", '{"name": "R", "node": "hub", "hand": "edited"}')
    _setup(_EXTEND_RUN, fleet_dir, wire)

    assert wire.flashed() == []
    assert json.loads(wire.on(HUB_PORT, "AlLoRa.json"))["hand"] == "edited"
    pushed_to_hub = [a for a in wire.calls
                     if HUB_PORT in " ".join(a) and "cp" in a]
    assert [a[-1] for a in pushed_to_hub] == [":Nodes.json"]


def test_an_added_edge_gets_a_name_the_fleet_does_not_already_hold(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir))
    _setup(_EXTEND_RUN, fleet_dir, wire)

    edges = [e for e in _registry(fleet_dir) if e["role"] == "edge"]
    assert sorted(e["name"] for e in edges) == ["S", "S2"]
    assert len(set(e["device_id"] for e in edges)) == 2


def test_a_hub_with_no_roster_is_not_a_hub_to_extend(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    # No Nodes.json placed: a board that this toolkit has never provisioned as a Hub.
    code, tail = _setup(_EXTEND_RUN, fleet_dir, wire)

    assert code != 0
    assert "holds no Nodes.json" in tail


def test_a_posture_change_warns_and_guides_rather_than_refusing(tmp_path, monkeypatch, capsys):
    """Refusing would send the operator to reflash every board by hand, one at a time."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT,
                    posture="open")
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir))
    capsys.readouterr()
    code, tail = _setup(("", "1", "y", "3", "", "", "", "", "", "", "", "", ""),
                        fleet_dir, wire)

    said = capsys.readouterr().out
    assert "this deployment is secure, and you have asked for open" in said
    assert "cannot talk to each other" in said
    assert code == 0, tail


def test_declining_the_posture_change_changes_nothing(tmp_path, monkeypatch):
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())
    before = _registry(fleet_dir)

    _ports(monkeypatch, EDGE_PORT, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT, EDGE_PORT), edge_port=THIRD_PORT)
    code, tail = _setup(("", "1", "n"), fleet_dir, wire)  # extend, open, do not go on

    assert code != 0
    assert "keep the secure posture" in tail
    assert _registry(fleet_dir) == before
    assert wire.on(THIRD_PORT, "AlLoRa.json") is None


def test_starting_a_new_deployment_never_writes_over_the_old_one(tmp_path, monkeypatch):
    """A fleet directory holds the root every running node is pinned to. It is not reused."""
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    _setup(_FIRST_RUN, fleet_dir, FakeWire())
    original = _registry(fleet_dir)
    root_before = (fleet_dir / "control_root.key").exists()

    wire = FakeWire()
    # scratch (option 2), the offered directory, then the ordinary first-run questions.
    code, tail = _setup(("2", "") + _FIRST_RUN, fleet_dir, wire)

    assert code == 0, tail
    assert _registry(fleet_dir) == original, "the running deployment was rewritten"
    assert (fleet_dir / "control_root.key").exists() == root_before
    assert _registry(tmp_path / "fleet-2"), "the new deployment went somewhere else"


def test_replacing_a_broken_node_keeps_its_name_and_takes_its_slot(tmp_path, monkeypatch):
    """The other thing extend covers. A replacement board is not a new node.

    Given a new name it would leave the fleet and the Hub's roster both carrying the record of
    a board that is dead, and the Hub polls that fingerprint a whole listening window at a time.
    """
    _ports(monkeypatch, EDGE_PORT, HUB_PORT)
    fleet_dir = tmp_path / "fleet"
    code, tail = _setup(_FIRST_RUN, fleet_dir, FakeWire())
    assert code == 0, tail
    dead = DEVICE_IDS[EDGE_PORT]

    # S's board died. The third board takes its place, and answers on the same slot.
    _ports(monkeypatch, HUB_PORT, THIRD_PORT)
    wire = FakeWire(ports=(THIRD_PORT, HUB_PORT), edge_port=THIRD_PORT)
    wire.place(HUB_PORT, "Nodes.json", _hub_roster(fleet_dir))
    # extend, posture, which board is the new Edge (2, the third board), replacement (2, which
    # is "replacing S"), firmware, radio, sf, freq, bandwidth, proceed. Two boards on the desk,
    # so there is no count question and the Hub is the one left over.
    code, tail = _setup(("", "", "2", "2", "", "", "", "", "", ""), fleet_dir, wire)
    assert code == 0, tail

    edges = [e for e in _registry(fleet_dir) if e["role"] == "edge"]
    assert [e["name"] for e in edges] == ["S"], "the dead board kept a record of its own"
    assert edges[0]["device_id"] == DEVICE_IDS[THIRD_PORT]

    roster = json.loads(wire.on(HUB_PORT, "Nodes.json"))
    assert [e["name"] for e in roster] == ["S"]
    assert roster[0]["device_id"] == DEVICE_IDS[THIRD_PORT]
    assert dead not in json.dumps(roster), "the Hub would poll a fingerprint nobody holds"

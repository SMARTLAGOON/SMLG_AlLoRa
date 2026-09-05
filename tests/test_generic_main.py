"""One program for every node, and the longhand copy that must not drift from it.

`examples/v3_hello/main.py` is the file every provisioned board runs. It reads the config, builds
what the config names, and runs it, so what distinguishes an Edge from a Hub, open from control,
and one radio from another is the JSON beside it rather than a fork of the program.

That is what closes the radio bug. The Edge used to be given a per-posture example whose first
import named `SX127x_connector`, while the Hub was given a template with the radio substituted
in. One path honoured the operator's choice and one ignored it, so asking for an SX1262 Edge
produced an SX127x one, and verification passed because both boards were provisioned the same
wrong way. There is no longer a program with a radio in it.

`secure/edge/main_literal.py` is the same deployment written out by hand, for a reader who does
not want to follow a dispatch and for anyone holding a radio this repository has never supported.
The last test here runs both and requires the same node out of each, because a literal file that
has drifted is worse than no literal file: it is the one people read.
"""
import importlib.util
import json
import os
import time

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink

_EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "v3_hello")
EDGE_MAC = "a1a1a1a1"


def _load_generic():
    """Import examples/v3_hello/main.py without running it (the __main__ guard is what allows
    this, and a board still runs the file as __main__)."""
    spec = importlib.util.spec_from_file_location(
        "v3_hello_main", os.path.join(_EXAMPLES, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generic = _load_generic()


def _config(node="edge", **overrides):
    config = {
        "name": "S",
        "node": node,
        "mesh_mode": False,
        "short_mac": True,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": 42,
        "debug": False,
        "connector": {
            "driver": "sx127x",
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    if node == "hub":
        config["result_path"] = "Results"
    else:
        config["queue_path"] = "Outbox"
    config.update(overrides)
    return config


def _write(tmp_path, config, name="AlLoRa.json"):
    with open(str(tmp_path / name), "w") as f:
        f.write(json.dumps(config, indent=2))
    return name


def _build(tmp_path, monkeypatch, config, name="AlLoRa.json"):
    _write(tmp_path, config, name)
    monkeypatch.chdir(tmp_path)
    return generic.build_node(config, name, Loopback_connector(EDGE_MAC))


# --- the radio comes from the config ---------------------------------------------------------

def test_an_unknown_driver_halts_and_names_the_ones_it_builds():
    """Never a fallback to a default radio. Coming up on the wrong chip by way of a typo is the
    same failure the driver key exists to close, reached from a different direction."""
    with pytest.raises(SystemExit) as excinfo:
        generic.build_connector("sx1280")
    message = str(excinfo.value)
    assert "sx1280" in message
    for known in ("sx127x", "sx1262", "e5", "lopy4"):
        assert known in message


def test_every_radio_the_wizard_offers_is_a_radio_this_program_can_build():
    """The two lists are written in different files and would drift apart silently: an operator
    would pick a radio the wizard accepts and the board would halt on it at boot."""
    from tools.allora_provision.node_config import DRIVERS
    source = open(os.path.join(_EXAMPLES, "main.py")).read()
    for driver in DRIVERS:
        assert '"{}"'.format(driver) in source, driver


def test_the_wizard_writes_the_radio_it_was_asked_for_into_the_config():
    """Every pair the wizard offers builds a config naming that pair's radio.

    Iterated over the board-and-radio pairs rather than over the radios alone, because a radio
    is only provisionable on a board that carries it: an E5 on a T3-S3 is a real combination
    nobody has recorded the pins for, and a LoPy4 is not a chip that board can have at all.
    Both are refused deliberately, and a hand-written deployment reaches them anyway, which is
    why `DRIVERS` above stays the full vocabulary.
    """
    from tools.allora_provision.node_config import build_lora_json
    from tools.allora_provision.setup import hardware_options
    offered = hardware_options()
    assert offered, "the wizard offers no hardware at all"
    for board, driver in offered:
        for role in ("edge", "hub"):
            config = build_lora_json(role=role, posture="secure", driver=driver, board=board)
            assert config["connector"]["driver"] == driver


# --- the config says what the board is -------------------------------------------------------

def test_an_edge_config_builds_an_edge_serving_from_the_folder_it_names(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch, _config("edge", queue_path="Queue"))
    assert isinstance(node, Edge)
    assert isinstance(node.datasource, Disk_DataSource)
    assert node.datasource.queue_path == "Queue"


def test_an_edge_with_no_queue_path_falls_back_to_internal_flash(tmp_path, monkeypatch):
    config = _config("edge")
    del config["queue_path"]
    node = _build(tmp_path, monkeypatch, config)
    assert node.datasource.queue_path == "Outbox"


def test_a_hub_config_builds_a_hub(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch, _config("hub"))
    assert isinstance(node, Hub)


def test_a_config_declaring_nothing_is_refused_rather_than_guessed(tmp_path, monkeypatch):
    """Guessing here would put a node on the air in a placement nobody chose."""
    config = _config("edge")
    del config["node"]
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, monkeypatch, config)
    assert "node" in str(excinfo.value) and "adapter" in str(excinfo.value)


def test_a_bridge_config_is_sent_to_the_adapter_program(tmp_path, monkeypatch):
    """A bridge is not a third kind of node: it holds no protocol logic, no keys and no session.
    Which key is present is the declaration, so this program says so and stops."""
    config = _config("edge")
    del config["node"]
    config["adapter"] = {"transport": "wifi"}
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, monkeypatch, config)
    assert "bridge" in str(excinfo.value)


def test_an_unknown_node_kind_is_refused(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _build(tmp_path, monkeypatch, _config("gateway"))


# --- the posture comes from what the node came up holding ------------------------------------

def test_a_node_with_no_root_acts_on_unsigned_commands(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch, _config("edge"))
    generic.wire_control(node)
    assert node.control_actuator is not None
    assert not isinstance(node.data_sink, Control_Root_DataSink)


def test_a_rooted_node_with_an_identity_verifies_against_the_root(tmp_path, monkeypatch):
    from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
    import os as _os
    priv = generate_private_key(_os.urandom)
    with open(str(tmp_path / "control_root.key"), "w") as f:
        f.write(public_key_uncompressed(priv).hex())
    config = _config("edge", security_mode="secure",
                     identity_file="identity.key",
                     control_root_file="control_root.key")
    del config["session_id"]
    node = _build(tmp_path, monkeypatch, config)
    generic.wire_control(node)
    assert isinstance(node.data_sink, Control_Root_DataSink)


def test_a_rooted_node_with_no_identity_halts_instead_of_looking_provisioned(
        tmp_path, monkeypatch):
    """It could refuse unsigned commands and never verify a signed one, because a signed
    artifact is addressed to a device_id an open node does not have. Left running it looks
    provisioned and can never accept a command."""
    from AlLoRa.Security.ec_p256 import generate_private_key, public_key_uncompressed
    import os as _os
    priv = generate_private_key(_os.urandom)
    with open(str(tmp_path / "control_root.key"), "w") as f:
        f.write(public_key_uncompressed(priv).hex())
    node = _build(tmp_path, monkeypatch,
                  _config("edge", control_root_file="control_root.key"))
    with pytest.raises(SystemExit) as excinfo:
        generic.wire_control(node)
    assert "identity" in str(excinfo.value)


# --- a refusal reaches the operator, and the board stays stopped ------------------------------

class _Woke(Exception):
    """Stands in for the next tick of a halt that would otherwise never return."""


def _sleep_once_then_wake(recorded):
    def _sleep(seconds):
        recorded.append(seconds)
        if len(recorded) == 3:
            raise _Woke()
    return _sleep


def test_a_stopped_node_keeps_saying_why_instead_of_going_quiet(monkeypatch, capsys):
    """Every refusal in this program carries a sentence written for whoever fixes the config.
    MicroPython prints nothing for SystemExit, so the sentence has to be printed here or it
    reaches nobody, and it is repeated because the cable is usually attached after the fact."""
    slept = []
    monkeypatch.setattr(generic.time, "sleep", _sleep_once_then_wake(slept))

    with pytest.raises(_Woke):
        generic.halt("the card would not mount", interval=7)

    assert capsys.readouterr().out.count("the card would not mount") == 3
    assert slept == [7, 7, 7]


def test_a_refused_config_halts_instead_of_handing_a_reboot_back_to_the_runtime(monkeypatch):
    """An uncaught SystemExit out of main.py is a soft reset on the ESP32 port, so the board
    re-reads the same bad config and refuses again, forever. The entry catches it instead."""
    def _refuse():
        raise SystemExit("Nodes.json registered no active endpoint")

    stopped = []
    monkeypatch.setattr(generic, "main", _refuse)
    monkeypatch.setattr(generic, "halt", lambda reason, **kw: stopped.append(reason))

    generic.run()

    assert stopped == ["Nodes.json registered no active endpoint"]


def test_a_refusal_with_nothing_to_say_still_stops_rather_than_rebooting(monkeypatch):
    """SystemExit() with no argument is still a stop, and still must not become a reboot."""
    def _refuse():
        raise SystemExit()

    stopped = []
    monkeypatch.setattr(generic, "main", _refuse)
    monkeypatch.setattr(generic, "halt", lambda reason, **kw: stopped.append(reason))

    generic.run()

    assert stopped == ["stopped, and gave no reason"]


# --- the longhand copy has not drifted -------------------------------------------------------

def test_the_literal_example_builds_the_same_node_as_the_dispatch(tmp_path, monkeypatch):
    """Runs secure/edge/main_literal.py with a loopback in place of its radio and stops it
    before its loop, then builds the same config through the generic program. Both have to
    produce the same node: same class, same data source, same folder, same control wiring."""
    import sys
    import types

    config = _config("edge", security_mode="secure", identity_file="identity.key")
    del config["session_id"]
    _write(tmp_path, config)
    monkeypatch.chdir(tmp_path)

    # Stand in for the radio module, which imports MicroPython-only names and cannot be loaded
    # here. Everything else in the literal file runs exactly as it is written.
    stub = types.ModuleType("AlLoRa.Connectors.SX127x_connector")
    stub.SX127x_connector = lambda: Loopback_connector(EDGE_MAC)
    monkeypatch.setitem(sys.modules, "AlLoRa.Connectors.SX127x_connector", stub)

    built = {}
    monkeypatch.setattr(Edge, "run", lambda self, *a, **kw: built.setdefault("node", self))

    source = open(os.path.join(_EXAMPLES, "secure", "edge", "main_literal.py")).read()
    namespace = {"__name__": "__main__"}
    exec(compile(source, "main_literal.py", "exec"), namespace)
    literal = built["node"]

    dispatched = generic.build_node(config, "AlLoRa.json", Loopback_connector(EDGE_MAC))
    generic.wire_control(dispatched)

    assert type(literal) is type(dispatched)
    assert type(literal.datasource) is type(dispatched.datasource)
    assert literal.datasource.queue_path == dispatched.datasource.queue_path
    assert literal.config_file == dispatched.config_file
    assert literal.security_mode == dispatched.security_mode
    assert type(literal.control_actuator) is type(dispatched.control_actuator)
    assert type(literal.data_sink) is type(dispatched.data_sink)


def test_the_longhand_copy_also_stays_stopped_rather_than_rebooting(tmp_path, monkeypatch, capsys):
    """The literal file is the one people copy when their deployment is not one the config can
    describe, so its two stops need the same treatment as the dispatch's ten. Given an open
    config it has no identity to run with, and it must say so rather than raise into a reboot."""
    import sys
    import types

    _write(tmp_path, _config("edge"))
    monkeypatch.chdir(tmp_path)

    stub = types.ModuleType("AlLoRa.Connectors.SX127x_connector")
    stub.SX127x_connector = lambda: Loopback_connector(EDGE_MAC)
    monkeypatch.setitem(sys.modules, "AlLoRa.Connectors.SX127x_connector", stub)

    slept = []
    monkeypatch.setattr(time, "sleep", _sleep_once_then_wake(slept))

    source = open(os.path.join(_EXAMPLES, "secure", "edge", "main_literal.py")).read()
    with pytest.raises(_Woke):
        exec(compile(source, "main_literal.py", "exec"), {"__name__": "__main__"})

    assert capsys.readouterr().out.count("This node has no identity") == 3


# --- a file actually crosses, served the way the config says ---------------------------------

def test_a_file_dropped_in_the_outbox_crosses_to_the_hub(tmp_path, monkeypatch):
    """The end of the chain, and the part construction alone does not prove. The Edge is given
    no file by any program: something puts one in the folder its config names, and the node
    serves it. This is the behaviour that replaced a program which generated its own payload,
    so it is the one worth pinning end to end."""
    import threading

    monkeypatch.chdir(tmp_path)
    edge_conn, hub_conn = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")

    payload = bytes(i % 256 for i in range(1000))
    os.mkdir(str(tmp_path / "Outbox"))
    with open(str(tmp_path / "Outbox" / "hello.bin"), "wb") as f:
        f.write(payload)

    edge_config = _config("edge", chunk_size=243)
    _write(tmp_path, edge_config, "edge.json")
    edge = generic.build_node(edge_config, "edge.json", edge_conn)

    hub_config = _config("hub", chunk_size=243, result_path=str(tmp_path / "Results"))
    _write(tmp_path, hub_config, "hub.json")
    hub = generic.build_node(hub_config, "hub.json", hub_conn)

    from AlLoRa.Digital_Endpoint import Digital_Endpoint
    endpoint = Digital_Endpoint(name="src", mac_address="a1a1a1a1", active=True, session_id=42)

    errors = []

    def serve():
        try:
            edge.run(timeout=30)
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="edge-serve", daemon=True)
    server.start()
    hub.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
    server.join(timeout=20)

    assert not errors, "edge thread raised: {}".format(errors)
    received = tmp_path / "Results" / "a1a1a1a1" / "hello.bin"
    assert received.exists(), "the hub never saved the file at {}".format(received)
    assert received.read_bytes() == payload


def test_an_edge_with_an_empty_outbox_waits_instead_of_inventing_something(
        tmp_path, monkeypatch):
    """The decision behind the outbox: a node with nothing to send has nothing to send. A
    program that generated a demo payload when its queue was empty would ship that behaviour to
    every board in a fleet."""
    monkeypatch.chdir(tmp_path)
    os.mkdir(str(tmp_path / "Outbox"))
    node = _build(tmp_path, monkeypatch, _config("edge"))
    node.run(timeout=1)
    assert not node.got_file()

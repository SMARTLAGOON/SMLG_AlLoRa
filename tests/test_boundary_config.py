"""A boundary is named in the config, the way a radio is.

`Node` has taken `datasource=` and `data_sink=` since v3 began, and until now no `.json` in the
repository named either one. So a deployment wanting its received files on a broker instead of a
card had to fork the program, which is why `MQTT_DataSink` sat built and tested from July to
September with no example: not a decision, just no way to select it without writing Python.

Three things are pinned here, and they are the three that can go quietly wrong.

**Absence changes nothing.** Every config file in the field, and every one vendored into a
student repository, has to keep producing the node it produces today. That is stronger than it
sounds because "today" is a different default in each of the three slots: the node builds its
disk sink lazily, an Edge builds its disk source eagerly, and a Hub has no downlink source for an
endpoint at all until something is queued.

**A mistake stops the boot.** An unknown kind, a mistyped key, a key already spelled at the top
level. Each of them otherwise produces a node that looks provisioned, delivers nowhere, and says
nothing about it, which is the failure the whole config-schema line of work exists to close.

**A rooted Edge keeps its verify gate.** The sink slot on a node holding a control root is the
signature check. A config key able to take that slot would let a text edit disarm it.
"""
import importlib.util
import json
import os

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.DataSources.MQTT_DataSource import MQTT_DataSource
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub

_EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "v3_hello")
EDGE_MAC = "a1a1a1a1"


def _load_generic():
    spec = importlib.util.spec_from_file_location(
        "v3_hello_main_boundaries", os.path.join(_EXAMPLES, "main.py"))
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


def _build(tmp_path, monkeypatch, config, name="AlLoRa.json", nodes=None):
    with open(str(tmp_path / name), "w") as f:
        f.write(json.dumps(config, indent=2))
    if nodes is not None:
        with open(str(tmp_path / "Nodes.json"), "w") as f:
            f.write(json.dumps(nodes, indent=2))
    monkeypatch.chdir(tmp_path)
    return generic.build_node(config, name, Loopback_connector(EDGE_MAC))


# --- absence changes nothing -------------------------------------------------------------

def test_a_hub_with_no_sink_block_is_the_hub_it_has_always_been(tmp_path, monkeypatch):
    """The acceptance criterion, and the reason the disk kind returns None rather than a sink:
    the node builds `Disk_DataSink(result_path)` lazily when a file first completes, so handing
    it one at construction would be a nearly identical node instead of an identical one."""
    node = _build(tmp_path, monkeypatch, _config("hub"), nodes=[])
    assert isinstance(node, Hub)
    assert node.data_sink is None


def test_naming_the_disk_sink_explicitly_is_the_same_as_not_naming_it(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch,
                  _config("hub", data_sink={"kind": "disk"}), nodes=[])
    assert node.data_sink is None


def test_an_edge_with_no_source_block_still_serves_from_its_outbox(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch, _config("edge", queue_path="Queue"))
    assert isinstance(node.datasource, Disk_DataSource)
    assert node.datasource.queue_path == "Queue"


def test_naming_the_disk_source_explicitly_still_uses_the_top_level_queue_path(
        tmp_path, monkeypatch):
    """`queue_path` stays the single home for the outbox folder: the block selects the class and
    the existing key says where it reads, so there is never a second place to look."""
    node = _build(tmp_path, monkeypatch,
                  _config("edge", queue_path="Queue", datasource={"kind": "disk"}))
    assert isinstance(node.datasource, Disk_DataSource)
    assert node.datasource.queue_path == "Queue"


# --- the config can now select the other boundary ----------------------------------------

def test_a_hub_config_can_put_its_received_files_on_a_broker(tmp_path, monkeypatch):
    """The whole point. Before this, selecting MQTT meant forking the program."""
    node = _build(tmp_path, monkeypatch,
                  _config("hub", data_sink={"kind": "mqtt", "host": "10.0.0.4",
                                            "topic_prefix": "albufera"}),
                  nodes=[])
    assert isinstance(node.data_sink, MQTT_DataSink)
    assert node.data_sink.host == "10.0.0.4"
    assert node.data_sink.topic_prefix == "albufera"


def test_keys_left_out_of_a_block_keep_the_classs_own_defaults(tmp_path, monkeypatch):
    """One source of truth for every default, and it is the constructor. A Hub sitting on the
    same machine as its broker is the ordinary case, not an exotic one."""
    node = _build(tmp_path, monkeypatch,
                  _config("hub", data_sink={"kind": "mqtt"}), nodes=[])
    assert node.data_sink.host == "localhost"
    assert node.data_sink.port == 1883
    assert node.data_sink.topic_prefix == "allora"


def test_an_edge_can_serve_from_a_broker_instead_of_a_folder(tmp_path, monkeypatch):
    node = _build(tmp_path, monkeypatch,
                  _config("edge", datasource={"kind": "mqtt", "topics": ["sensors/#"]}))
    assert isinstance(node.datasource, MQTT_DataSource)
    # JSON has no tuples and the source keeps one.
    assert node.datasource.topics == ("sensors/#",)


# --- a mistake stops the boot ------------------------------------------------------------

def test_an_unknown_kind_halts_and_names_the_ones_it_builds():
    """Never a fallback to disk. A config naming a boundary this program cannot build was
    written against a different program, and running it as disk gives a node that looks
    provisioned, delivers nowhere and reports nothing."""
    with pytest.raises(SystemExit) as excinfo:
        generic.build_data_sink({"kind": "influx"})
    message = str(excinfo.value)
    assert "influx" in message
    assert "disk" in message and "mqtt" in message


def test_a_block_with_no_kind_halts():
    with pytest.raises(SystemExit) as excinfo:
        generic.build_data_sink({"host": "10.0.0.4"})
    assert "kind" in str(excinfo.value)


def test_a_mistyped_key_halts_rather_than_silently_falling_back():
    """The guard that actually matters. `hosts` would otherwise be dropped and the sink would
    publish to localhost, which is a broker that usually exists and is never the right one."""
    with pytest.raises(SystemExit) as excinfo:
        generic.build_data_sink({"kind": "mqtt", "hosts": "10.0.0.4"})
    message = str(excinfo.value)
    assert "hosts" in message
    assert "host" in message


def test_a_key_the_config_already_spells_at_the_top_level_halts():
    """Two places to read the same setting is one place too many, and it raises an ordering
    question that has no good answer."""
    with pytest.raises(SystemExit) as excinfo:
        generic.build_data_sink({"kind": "disk", "result_path": "Elsewhere"})
    assert "result_path" in str(excinfo.value)

    with pytest.raises(SystemExit) as excinfo:
        generic.build_datasource({"kind": "disk", "queue_path": "Elsewhere"},
                                 reserved=("queue_path",))
    assert "queue_path" in str(excinfo.value)


def test_a_disk_block_takes_a_path_where_there_is_no_outer_key_to_defer_to():
    """The same rule read the other way. A roster entry has no `queue_path` above it, so the
    block carries its own: a Hub serving five Edges from five folders needs five paths."""
    source = generic.build_datasource({"kind": "disk", "queue_path": "downlink/A"})
    assert isinstance(source, Disk_DataSource)
    assert source.queue_path == "downlink/A"


# --- a rooted Edge keeps its verify gate -------------------------------------------------

def test_a_rooted_edge_that_also_names_a_sink_is_refused_at_boot(tmp_path, monkeypatch):
    """The sink slot on a node holding a control root is the signature check itself. Letting the
    config win would disarm it with a text edit; letting the gate win silently would leave an
    operator reading `mqtt` in a file that does nothing. So neither, and say so while somebody
    is still looking."""
    config = _config("edge",
                     control_root_file="control_root.pub",
                     data_sink={"kind": "mqtt", "host": "10.0.0.4"})
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, monkeypatch, config)
    message = str(excinfo.value)
    assert "control_root_file" in message and "data_sink" in message


def test_an_open_edge_may_name_a_sink_because_it_has_no_gate_to_lose(tmp_path, monkeypatch):
    """The refusal above is specific to a node that answers to an authority. On a survey link
    there is no gate, the slot is free, and relaying a received file onward is ordinary."""
    node = _build(tmp_path, monkeypatch,
                  _config("edge", data_sink={"kind": "mqtt", "host": "10.0.0.4"}))
    assert isinstance(node, Edge)
    assert isinstance(node.data_sink, MQTT_DataSink)


# --- a Hub's downlink is per Edge --------------------------------------------------------

def _entry(name, mac, **extra):
    entry = {"name": name, "mac_address": mac, "active": True, "sleep_mesh": False,
             "asking_frequency": 60, "listening_time": 30,
             "lock_on_file_receive": False, "max_listen_time_when_locked": 60}
    entry.update(extra)
    return entry


def test_a_datasource_at_the_top_of_a_hub_config_is_redirected_not_refused(
        tmp_path, monkeypatch):
    """A Hub serves a different downlink to each Edge, so one source at the top of its own file
    would say every Edge is served the same thing. The halt names the file to write instead,
    which is the difference between "you cannot" and "not here"."""
    with pytest.raises(SystemExit) as excinfo:
        _build(tmp_path, monkeypatch,
               _config("hub", datasource={"kind": "mqtt"}), nodes=[])
    message = str(excinfo.value)
    assert "Nodes.json" in message


def test_each_endpoint_gets_the_downlink_source_its_roster_entry_names(tmp_path, monkeypatch):
    """The case that made the key Edge-by-Edge rather than Hub-wide: different Edges are sent
    different artifacts, and `Hub._downlink` has always been keyed by session id."""
    nodes = [_entry("A", "aaaaaaaa", datasource={"kind": "disk", "queue_path": "downlink/A"}),
             _entry("B", "bbbbbbbb")]
    node = _build(tmp_path, monkeypatch, _config("hub"), nodes=nodes)
    notes = generic.wire_downlink_sources(node)

    by_label = {ep.get_label(): ep for ep in node.digital_endpoints}
    served = node._downlink[by_label["aaaaaaaa"].session_id]
    assert isinstance(served, Disk_DataSource)
    assert served.queue_path == "downlink/A"
    # B named nothing, so it keeps today's behaviour: no registered source, and the in-RAM
    # queue appears if and when queue_downlink puts something in it.
    assert by_label["bbbbbbbb"].session_id not in node._downlink
    assert any("A" in note for note in notes)


def test_an_entry_naming_a_source_that_is_not_a_registered_endpoint_is_reported(
        tmp_path, monkeypatch):
    """A block nobody will ever read, because the entry is inactive or its address was edited on
    one side only. Silence here is how a deployment ends up wondering why one Edge gets
    nothing."""
    nodes = [_entry("A", "aaaaaaaa"),
             _entry("Ghost", "cccccccc", active=False,
                    datasource={"kind": "disk", "queue_path": "downlink/G"})]
    node = _build(tmp_path, monkeypatch, _config("hub"), nodes=nodes)
    notes = generic.wire_downlink_sources(node)
    assert any("WARNING" in note and "cccccccc" in note for note in notes)


def test_a_roster_entry_carrying_a_boundary_block_survives_the_hubs_write_back(
        tmp_path, monkeypatch):
    """The roster is overlaid and never rebuilt from the live endpoints, precisely so an entry
    keeps keys the endpoint does not model. This is that guarantee, now load-bearing for a key
    that did not exist when it was written."""
    block = {"kind": "disk", "queue_path": "downlink/A"}
    nodes = [_entry("A", "aaaaaaaa", datasource=block)]
    node = _build(tmp_path, monkeypatch, _config("hub"), nodes=nodes)

    endpoint = node.digital_endpoints[0]
    endpoint.sf = (endpoint.sf or 7) + 1
    node._persist_endpoint_rf(endpoint)

    with open(str(tmp_path / "Nodes.json")) as f:
        written = json.loads(f.read())
    # The write-back really fired, so the survival below is a fact about the overlay and not
    # about a file nobody touched.
    assert written[0]["connector"]["sf"] == endpoint.sf
    assert written[0]["datasource"] == block


# --- the library still knows none of this ------------------------------------------------

def test_no_boundary_dispatch_leaked_into_the_library():
    """The no-framework line. A table of legal boundaries inside `AlLoRa/` would start deciding
    which compositions are blessed, and would drag paho into the import path of deployments that
    publish nothing."""
    package = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "AlLoRa")
    for folder, _, files in os.walk(package):
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name)) as f:
                body = f.read()
            assert '"data_sink"' not in body, os.path.join(folder, name)
            assert '"datasource"' not in body, os.path.join(folder, name)

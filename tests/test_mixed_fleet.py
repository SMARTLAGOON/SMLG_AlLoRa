"""One Hub polls a mixed fleet: re-flashed v3 Edges and un-reflashed v2 ones.

An endpoint's entry may say `protocol_version: 2`, and the Hub speaks v2 for that visit and
its own version again afterwards. An entry that says nothing is polled in the Hub's own
version, so every roster written before this existed means what it meant. A node registered
by device_id is v3 by what that registration is.

The end-to-end case puts a v3 Edge and the library as tagged v2.0.0 on one shared channel,
where each hears every frame and ignores what it cannot parse, as on air.
"""
import json
import queue
import threading

import pytest

from v2_tag import V2Peer, extract_v2_tree
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Codec import V2Codec
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint, Version_conflict
from AlLoRa.Links.Loopback_link import Loopback_link
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes import Hub as hub_module
from AlLoRa.Nodes import Node as node_module
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"
OLD_MAC = "a1a1a1a1"     # the v2.0.0 Edge
NEW_MAC = "c3c3c3c3"     # the v3 Edge
NEW_SID = 42
OLD_FILE = bytes(i % 256 for i in range(1000))
NEW_FILE = bytes((i * 7) % 256 for i in range(700))
_RF = {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1, "tx_power": 14,
       "timeout_delta": 3, "debug": False}


@pytest.fixture(scope="module")
def v2_tree(tmp_path_factory):
    return extract_v2_tree(tmp_path_factory.mktemp("v2.0.0"))


def _write(path, config):
    path.write_text(json.dumps(config))
    return str(path)


def _hub_config(tmp_path, protocol_version=3, security_mode="open"):
    return _write(tmp_path / "hub.json", {
        "name": "hub", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": protocol_version, "security_mode": security_mode, "session_id": 9,
        "debug": False, "result_path": str(tmp_path / "results"), "connector": _RF})


def _entry(name, mac, **more):
    entry = {"name": name, "mac_address": mac, "active": True,
             "wait_after_visit": 0, "listening_time": 4}
    entry.update(more)
    return entry


def _hub(tmp_path, entries, connector=None, protocol_version=3, security_mode="open"):
    nodes = _write(tmp_path / "Nodes.json", entries)
    return Hub(connector or Loopback_connector(HUB_MAC),
               config_file=_hub_config(tmp_path, protocol_version, security_mode),
               nodes_file=nodes)


# --- the roster ---------------------------------------------------------------------------

def test_an_entry_states_its_protocol_version_or_leaves_it_to_the_hub():
    assert Digital_Endpoint(config={"mac_address": OLD_MAC,
                                    "protocol_version": 2}).protocol_version == 2
    assert Digital_Endpoint(config={"mac_address": NEW_MAC}).protocol_version is None


def test_a_node_registered_by_device_id_cannot_be_declared_v2():
    with pytest.raises(Version_conflict):
        Digital_Endpoint(config={"device_id": "0a0b0c0d", "protocol_version": 2})


@pytest.mark.parametrize("value", ["2", 4, 1, True])
def test_a_protocol_version_that_is_not_2_or_3_is_refused(value):
    with pytest.raises(Version_conflict):
        Digital_Endpoint(config={"mac_address": OLD_MAC, "protocol_version": value})


def test_a_v2_hub_refuses_a_roster_that_asks_it_to_speak_v3(tmp_path):
    with pytest.raises(Version_conflict):
        _hub(tmp_path, [_entry("new", NEW_MAC, protocol_version=3)], protocol_version=2)


def test_a_secure_hub_refuses_a_v2_node(tmp_path):
    # Every v2 visit would open with a handshake that v2 firmware cannot answer, and the node
    # would read as unreachable forever. Refused at boot instead.
    with pytest.raises(Version_conflict):
        _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2)], security_mode="secure")


def test_a_hub_on_a_v3_tunnel_bridge_refuses_a_v2_node(tmp_path):
    # In v3 the tunnel never asks the bridge for its MAC, and v2 frames are addressed by one.
    radio = Loopback_connector(HUB_MAC)
    radio.config(dict(_RF, protocol_version=3, addressing="sid"))
    client_link, bridge_link = Loopback_link.create_pair()
    bridge = Adapter(radio, link=bridge_link)
    stop = threading.Event()
    pump = threading.Thread(target=lambda: bridge.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    try:
        with pytest.raises(Version_conflict):
            _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2)],
                 connector=Tunnel_connector(link=client_link))
    finally:
        stop.set()
        pump.join(timeout=2)


def test_a_refused_roster_file_adds_none_of_its_entries(tmp_path):
    hub = _hub(tmp_path, [_entry("old", OLD_MAC)], protocol_version=2)
    more = _write(tmp_path / "more.json", [_entry("old", OLD_MAC), _entry("spare", "d4d4d4d4"),
                                           _entry("new", NEW_MAC, protocol_version=3)])
    with pytest.raises(Version_conflict):
        hub.add_digital_endpoints(more)
    assert [ep.get_name() for ep in hub.digital_endpoints] == ["old"]


def test_switching_on_a_v2_node_a_secure_hub_cannot_poll_leaves_it_off(tmp_path):
    # An inactive entry is never parsed at boot, so the site switching it on is the first time
    # its version is checked. Refused there, without stopping the Hub, and without writing the
    # file into a state the next boot would refuse.
    hub = _hub(tmp_path, [_entry("new", NEW_MAC),
                          _entry("old", OLD_MAC, protocol_version=2, active=False)],
               security_mode="secure")
    hub.submit_intent({"nodes": {OLD_MAC: {"active": True}}})

    hub._drain_management()

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["new"]
    roster = json.loads((tmp_path / "Nodes.json").read_text())
    assert [e["active"] for e in roster if e["mac_address"] == OLD_MAC] == [False]


def test_switching_on_an_entry_with_an_unreadable_version_does_not_stop_the_hub(tmp_path,
                                                                                monkeypatch):
    now = [0]
    monkeypatch.setattr(hub_module, "time", lambda: now[0])
    monkeypatch.setattr(hub_module, "sleep", lambda s: now.__setitem__(0, now[0] + 1000))
    hub = _hub(tmp_path, [_entry("new", NEW_MAC),
                          _entry("old", OLD_MAC, protocol_version="2", active=False)])
    hub.listen_to_endpoint = lambda *a, **k: True
    hub.submit_intent({"nodes": {OLD_MAC: {"active": True}}})

    hub.run(timeout=3)      # returns at its timeout, rather than raising out of the loop

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["new"]


def test_a_downlink_to_a_v2_node_is_refused_when_it_is_queued_or_plugged(tmp_path):
    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2)])
    old = hub.digital_endpoints[0]
    with pytest.raises(ValueError):
        hub.queue_downlink(old, AlLoRa_File(name="down.bin", content=bytearray(b"x" * 10),
                                            chunk_size=hub.get_chunk_size()))

    class Live_source:
        def prepare(self):
            raise AssertionError("brought up before the refusal")
    with pytest.raises(ValueError):
        hub.set_downlink_source(old, Live_source())


def test_a_radio_change_for_a_v2_node_is_refused(tmp_path):
    # Both routes a v3 Hub has for this speak v3, which the node would not understand.
    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2)])
    with pytest.raises(ValueError):
        hub.ask_change_rf(hub.digital_endpoints[0], {"sf": 9})


# --- the visit ----------------------------------------------------------------------------

def test_the_hub_speaks_v2_for_a_v2_visit_and_its_own_version_after(tmp_path, monkeypatch):
    now = [0]
    monkeypatch.setattr(hub_module, "time", lambda: now[0])
    monkeypatch.setattr(hub_module, "sleep", lambda s: now.__setitem__(0, now[0] + 1000))
    monkeypatch.setattr(node_module, "sleep", lambda s: None)
    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2),
                          _entry("new", NEW_MAC, session_id=NEW_SID)])
    home_codec, home_chunk = hub.connector.codec, hub.chunk_size
    seen = []

    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False, stall_timeout=None):
        hub.prepare_connector(digital_endpoint)     # what a real visit does first
        seen.append((digital_endpoint.get_name(), hub.protocol_version,
                     hub.connector.codec, hub.chunk_size))
        now[0] += 1000
        return True
    hub.listen_to_endpoint = listen

    hub.run(timeout=4)

    old = [(v, c, k) for name, v, c, k in seen if name == "old"]
    new = [(v, c, k) for name, v, c, k in seen if name == "new"]
    assert old and new
    # v2 METADATA does not carry a chunk size, so the Hub assumes what a v2 node would cut at:
    # 255 less the 20-byte long-MAC header.
    assert all(v == 2 and isinstance(c, V2Codec) and k == 235 for v, c, k in old)
    # The Hub's own codec object comes back, not a rebuilt one: a secure Hub keeps its sessions
    # on it, and a v2 visit in between must not cost every v3 node a fresh handshake.
    assert all(v == 3 and c is home_codec and k == home_chunk for v, c, k in new)
    assert hub.protocol_version == 3 and hub.connector.codec is home_codec


def test_a_v2_visit_cuts_chunks_for_the_nodes_own_spreading_factor(tmp_path, monkeypatch):
    # A v2 node at SF12 carries 30 bytes a frame, 10 of them payload beside a long-MAC header.
    # The Hub was last tuned elsewhere, so the guess has to follow the retune, not precede it.
    monkeypatch.setattr(node_module, "sleep", lambda s: None)
    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2, connector={"sf": 12})])
    seen = []

    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False, stall_timeout=None):
        hub.prepare_connector(digital_endpoint)
        seen.append((hub.connector.sf, hub.chunk_size))
        return True
    hub.listen_to_endpoint = listen

    hub._visit(hub.digital_endpoints[0], False, False)

    assert seen == [(12, 10)]


def test_a_v2_link_keeps_what_it_learned_from_one_visit_to_the_next(tmp_path, monkeypatch):
    # Switching version republishes the frame size, which re-bounds whatever pacing the radio
    # holds at that moment. Done in the wrong order it reset the link's window to its maximum
    # twice a visit and left v3 bounds on a v2 link, so a v2 node never got any faster.
    monkeypatch.setattr(node_module, "sleep", lambda s: None)
    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2, connector={"sf": 12})])
    old = hub.digital_endpoints[0]
    seen = []

    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False, stall_timeout=None):
        hub.prepare_connector(digital_endpoint)
        seen.append((hub.pacing.window, hub.pacing.min_timeout, hub.pacing.max_timeout))
        hub.pacing.window = hub.pacing.min_timeout      # this visit learned a tight window
        return True
    hub.listen_to_endpoint = listen

    hub._visit(old, False, False)
    learned = old.pacing.window
    hub._visit(old, False, False)

    assert seen[1][0] == learned
    assert (old.pacing.min_timeout, old.pacing.max_timeout) == seen[0][1:]


# --- end to end ---------------------------------------------------------------------------

def test_one_hub_pulls_from_a_v3_edge_and_a_v2_0_0_edge_in_one_run(tmp_path, v2_tree):
    hub_inbox, hub_outbox, new_inbox = queue.Queue(), queue.Queue(), queue.Queue()
    log = []

    old_file = tmp_path / "old.bin"
    old_file.write_bytes(OLD_FILE)
    old_config = _write(tmp_path / "old.json", {
        "name": "old", "chunk_size": 243, "mesh_mode": False, "short_mac": False,
        "debug": False, "connector": _RF})
    old = V2Peer(tmp_path, v2_tree, "edge", old_config, OLD_MAC, str(old_file), log,
                 hub_inbox.put)

    new_config = _write(tmp_path / "new.json", {
        "name": "new", "chunk_size": 243, "mesh_mode": False, "protocol_version": 3,
        "security_mode": "open", "session_id": NEW_SID, "debug": False, "connector": _RF})
    new = Edge(Loopback_connector(NEW_MAC, inbox=new_inbox, outbox=hub_inbox),
               config_file=new_config)
    new.set_file(AlLoRa_File(name="new.bin", content=bytearray(NEW_FILE),
                             chunk_size=new.get_chunk_size()))
    serving = threading.Thread(target=new.send_file, kwargs={"timeout": 30}, daemon=True)
    serving.start()

    def channel():
        # Every frame the Hub sends reaches both Edges, as on air.
        while True:
            wire = hub_outbox.get()
            if wire is None:
                return
            new_inbox.put(wire)
            old.send(wire)
    on_air = threading.Thread(target=channel, daemon=True)
    on_air.start()

    hub = _hub(tmp_path, [_entry("old", OLD_MAC, protocol_version=2),
                          _entry("new", NEW_MAC, session_id=NEW_SID)],
               connector=Loopback_connector(HUB_MAC, inbox=hub_inbox, outbox=hub_outbox))
    try:
        hub.run(timeout=12, save_files=True)
        assert old.finish(timeout=15) == 0, old.explain("did not finish serving its file")
    finally:
        hub_outbox.put(None)
        on_air.join(timeout=5)
        old.kill()
    serving.join(timeout=5)

    results = tmp_path / "results"
    assert (results / OLD_MAC / "old.bin").read_bytes() == OLD_FILE
    assert (results / NEW_MAC / "new.bin").read_bytes() == NEW_FILE

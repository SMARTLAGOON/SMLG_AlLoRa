"""Config persistence: a node writes back the config file it read.

One rule, both node kinds. An Edge persists its own radio to `LoRa.json` (`backup_config`);
a Hub persists an endpoint's settled radio to the `Nodes.json` it registered that endpoint
from. Either half missing loses the fleet: the two ends stop agreeing about which config is
in force, and only one of them wrote its answer down.

Defects this pins:
  1. Node.backup_config rebuilt a hand-picked subset, dropping protocol_version /
     security_mode / session_id / result_path / identity_file: a secure v3 node came back as
     an open v2 node off its own network.
  2. The connector block came from connector.backup_config(), which returned the STALE
     config_parameters dict: a change_rf_config (the committed trial) never reached it, so
     the node rebooted on the OLD radio config.
  3. The Hub wrote nothing at all. It commanded a retune, the Edge accepted and committed it,
     the Hub reported `accepted` — and a restart brought it back polling the old config while
     the Edge sat on the new one, with neither side able to return.

A bridge board persists nothing at runtime, so it has no backup_config; what matters for one
is that it reads its config the same way a node does. Seam B covers that.
"""
import json

from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes import Node as node_module
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub


def _connector_config():
    return {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "min_timeout": 0.5, "max_timeout": 12, "debug": False}


# --- fault injection: the two ways a config write can fail on a board ----------------------

class _Fat_os:
    """A filesystem that refuses to rename onto a name already in use, the way MicroPython's
    FAT does (littlefs replaces the target instead). A board built either way has to end up
    with the same file, or the write silently does nothing on half the fleet."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def rename(self, src, dst):
        try:
            open(dst).close()
        except OSError:
            return self._real.rename(src, dst)
        raise OSError("FR_EXIST: {} already exists".format(dst))


class _Half_written_file:
    """A file that takes half the bytes it is given and then dies, which is what a board
    losing power part way through a write leaves behind."""

    def __init__(self, handle):
        self._handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._handle.close()
        return False

    def write(self, data):
        self._handle.write(data[:len(data) // 2])
        raise OSError("power lost mid-write")


def _dying_open(path, mode="r"):
    handle = open(path, mode)
    return _Half_written_file(handle) if "w" in mode else handle


# --- Seam C: the connector reports its LIVE RF config, under the canonical LoRa.json keys --

def test_connector_backup_reflects_the_live_rf_config():
    conn = Loopback_connector("a1a1a1a1")
    conn.config(_connector_config())
    # A committed trial moved the radio off the configured values.
    conn.change_rf_config(frequency=915, sf=9, bw=250, cr=2, tx_power=20)

    backup = conn.backup_config()

    assert backup["sf"] == 9
    assert backup["freq"] == 915
    assert backup["bandwidth"] == 250
    assert backup["coding_rate"] == 2
    assert backup["tx_power"] == 20
    # Non-RF connector keys are preserved verbatim.
    assert backup["min_timeout"] == 0.5
    assert backup["max_timeout"] == 12


# --- Seam A: Node.backup_config round-trips the whole LoRa.json ----------------------------

def _write_v3_config(path, extra_top=None):
    config = {
        "name": "S", "chunk_size": 200, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 42,
        "result_path": "Results", "debug": False,
        "connector": _connector_config(),
    }
    if extra_top:
        config.update(extra_top)
    with open(path, "w") as f:
        json.dump(config, f)
    return config


def test_node_backup_config_preserves_the_v3_posture_and_persists_the_live_rf(tmp_path):
    path = str(tmp_path / "LoRa.json")
    _write_v3_config(path, extra_top={"custom_field": "keep-me"})

    node = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    node.change_rf_config({"sf": 9})     # a committed trial moves sf 7 -> 9
    node.backup_config()

    reloaded = json.load(open(path))
    # (a) the v3 / secure / identity posture and every other top-level key survive.
    assert reloaded["protocol_version"] == 3
    assert reloaded["security_mode"] == "open"
    assert reloaded["session_id"] == 42
    assert reloaded["result_path"] == "Results"
    assert reloaded["short_mac"] is True
    assert reloaded["custom_field"] == "keep-me"
    # (b) the connector block reflects the live RF (sf 9), other connector keys intact.
    assert reloaded["connector"]["sf"] == 9
    assert reloaded["connector"]["min_timeout"] == 0.5


def test_a_node_rebuilt_from_the_backup_still_boots_as_v3(tmp_path):
    # The whole point: a reconfig + reboot must not silently downgrade a secure v3 node to
    # an open v2 node off its own network.
    path = str(tmp_path / "LoRa.json")
    _write_v3_config(path)

    Edge(Loopback_connector("a1a1a1a1"), config_file=path).backup_config()

    rebuilt = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    assert rebuilt.protocol_version == 3
    assert rebuilt.session_id == 42


def test_a_backup_that_dies_part_way_leaves_the_node_bootable(tmp_path, monkeypatch):
    # Same rule at the other end, and the worse blast radius of the two: a node that loses
    # LoRa.json mid-write is in the field, off its own network, and cannot be recovered over
    # the air. The half-written bytes have to land somewhere other than the file it boots from.
    path = str(tmp_path / "LoRa.json")
    config = _write_v3_config(path)
    node = Edge(Loopback_connector("a1a1a1a1"), config_file=path)
    node.change_rf_config({"sf": 9})
    monkeypatch.setattr(node_module, "open", _dying_open, raising=False)

    try:
        node.backup_config()
    except OSError:
        pass                     # the write failed; what matters is what it left behind
    monkeypatch.undo()

    assert json.load(open(path)) == config, "the config the node boots from is untouched"
    assert Edge(Loopback_connector("a1a1a1a1"), config_file=path).protocol_version == 3


# --- Seam B: a bridge reads the same LoRa.json, and never writes to it ---------------------

def test_an_adapter_reads_its_v3_posture_and_link_block_without_persisting(tmp_path):
    path = str(tmp_path / "LoRa.json")
    config = {
        "name": "T", "chunk_size": 235, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 7, "debug": False,
        "connector": _connector_config(),
        "adapter": {"uartid": 0, "baud": 9600},
    }
    with open(path, "w") as f:
        json.dump(config, f)

    seen = {}

    class _Probe(Adapter):
        def setup_link(self, cfg):
            seen.update(cfg)

    radio = Loopback_connector("c3c3c3c3")
    _Probe(radio, config_file=path)

    # The v3 posture reaches the radio, and the link block reaches the medium.
    assert radio.protocol_version == 3 and radio.addressing == "sid"
    assert seen == {"uartid": 0, "baud": 9600}
    # And the file is untouched: a bridge holds no state worth persisting, so the whole
    # round-trip hazard above simply does not apply to it.
    assert json.load(open(path)) == config


# --- Seam D: the Hub writes an endpoint's settled RF back to Nodes.json --------------------

HUB_MAC = "b2b2b2b2"
EDGE_MAC = "a1a1a1a1"


def _write_hub_config(path):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 9, "debug": False,
        "connector": _connector_config(),
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _entry(name, mac="a1a1a1a1", **overrides):
    node = {"name": name, "mac_address": mac, "active": True,
            "asking_frequency": 60, "listening_time": 30}
    node.update(overrides)
    return node


def _hub(tmp_path, roster, **kwargs):
    """A Hub registered from a roster file on disk, plus the path to that file."""
    hub_config = str(tmp_path / "hub.json")
    _write_hub_config(hub_config)
    nodes_file = str(tmp_path / "Nodes.json")
    with open(nodes_file, "w") as f:
        json.dump(roster, f)
    kwargs.setdefault("nodes_file", nodes_file)
    hub = Hub(Loopback_connector(HUB_MAC), config_file=hub_config, **kwargs)
    return hub, nodes_file


def _reboot(hub, nodes_file):
    """The same Hub, restarted: it reads the same two files and nothing else."""
    return Hub(Loopback_connector(HUB_MAC), config_file=hub.config_file,
               nodes_file=nodes_file)


def _persisted(nodes_file, name):
    for node in json.load(open(nodes_file)):
        if node["name"] == name:
            return node
    return None


def _arm(hub, endpoint, sf=9):
    """Mirror the endpoint onto a new config and consume the one-visit "fresh" skip, which
    leaves the pair exactly where a real trial starts: the Hub's view on the new config, the
    old config retained as the probe fallback."""
    hub._mirror_endpoint_config(endpoint, {"sf": sf, "trial": 30})
    hub._probe_visit_end(endpoint, heard=False, completed=False)


def test_a_committed_change_survives_the_hubs_restart(tmp_path):
    # The defect, end to end: the Edge proved the new config with a full exchange, so the
    # probe settles the trial there. A Hub restarted after that must come up polling where it
    # left its Edge, not where the roster was hand-written months ago.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert _persisted(nodes_file, "edge")["connector"]["sf"] == 9
    assert _reboot(hub, nodes_file).digital_endpoints[0].sf == 9, \
        "the restarted Hub polls the config its Edge committed to"


def test_a_hub_restarted_mid_trial_comes_back_where_its_edge_is_heading(tmp_path):
    # The reason the write waits for the trial to resolve, and the one case that makes the
    # tempting implementation (write when the Hub's view moves, while the outcome is still
    # pending) worse than writing nothing.
    #
    # A Hub that restarts mid-trial stops giving the Edge the full-payload exchange its trial
    # needs to commit, so the Edge's window expires and it restores the old config. A Hub that
    # persisted nothing comes back on the old config too, and the pair meets unaided. A Hub
    # that persisted the unproven config comes back on the new one while the Edge returns to
    # the old one, and THAT split does not heal.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)
    assert endpoint.sf == 9, "the Hub's own view has already moved to the new config"

    hub._probe_visit_end(endpoint, heard=True, completed=False)   # located, nothing proved yet

    assert "connector" not in _persisted(nodes_file, "edge"), \
        "a config still on trial is not written down"
    assert _reboot(hub, nodes_file).digital_endpoints[0].sf == 7, \
        "a Hub restarted mid-trial polls the old config, which is where the Edge is heading"


def test_an_edge_that_rolled_back_leaves_the_roster_alone(tmp_path):
    # The endpoint settles on the config the file already describes. Nothing to write, and in
    # particular no connector block: an entry that states no RF means "poll me wherever you
    # already are", and an Edge that rolled back never moved away from that.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)],
                           probe_swap_after=1, probe_give_up_after=4)
    endpoint = hub.digital_endpoints[0]
    before = _persisted(nodes_file, "edge")
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=False, completed=False)  # silent on new: swap to old
    hub._probe_visit_end(endpoint, heard=True, completed=False)   # the Edge answers on old

    assert endpoint.sf == 7 and hub.endpoint_trial_old(endpoint) is None
    assert _persisted(nodes_file, "edge") == before, "an endpoint that did not move is not pinned"


def test_a_probe_that_gives_up_leaves_the_roster_alone(tmp_path):
    # Neither config ever answers, so the probe restores the old one and stops. Same reasoning:
    # the endpoint is back where the file had it, so the file is already right.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)],
                           probe_swap_after=1, probe_give_up_after=4)
    endpoint = hub.digital_endpoints[0]
    before = _persisted(nodes_file, "edge")
    _arm(hub, endpoint)

    for _ in range(4):
        hub._probe_visit_end(endpoint, heard=False, completed=False)

    assert endpoint.sf == 7 and hub.endpoint_trial_old(endpoint) is None
    assert _persisted(nodes_file, "edge") == before


def test_the_write_keeps_the_entries_and_keys_the_hub_does_not_model(tmp_path):
    # The roster is overlaid, never rebuilt from the live endpoints. An endpoint keeps only the
    # keys it models and drops the rest, and an inactive entry never becomes an endpoint at
    # all, so a rebuild would quietly delete three of the four entries in the shipped example
    # along with every key the class does not know about.
    roster = [
        _entry("spare", "c1c1c1c1", active=False, comment="kept for the winter deployment"),
        _entry("edge", EDGE_MAC, lock_on_file_receive=True, operator_note="roof, mast 2"),
        _entry("gps", "d1d1d1d1", active=False),
    ]
    hub, nodes_file = _hub(tmp_path, roster)
    endpoint = hub.digital_endpoints[0]
    assert endpoint.get_name() == "edge", "only the active entry became an endpoint"
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    written = json.load(open(nodes_file))
    assert [node["name"] for node in written] == ["spare", "edge", "gps"], \
        "the inactive entries survive a write they had no part in"
    assert written[0]["comment"] == "kept for the winter deployment"
    assert written[1]["operator_note"] == "roof, mast 2", "unmodelled keys survive the round trip"
    assert written[1]["lock_on_file_receive"] is True
    assert written[1]["connector"]["sf"] == 9


def test_the_write_fills_the_whole_connector_block_in_lora_json_spelling(tmp_path):
    # What the Hub writes is what an operator can paste into the peer's own LoRa.json, and it
    # is complete: an endpoint pinned by three of five settings would follow this Hub's default
    # for the other two, and a later change to that default would move it silently.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert _persisted(nodes_file, "edge")["connector"] == {
        "freq": 868, "sf": 9, "bandwidth": 125, "coding_rate": 1, "tx_power": 14}


def test_an_entry_that_already_states_the_new_config_is_left_untouched(tmp_path):
    # Nothing to record, so nothing is written: a Hub polling a fleet must not rewrite its
    # roster on every settled trial when the file already says the right thing.
    stated = {"freq": 868, "sf": 9, "bandwidth": 125, "coding_rate": 1, "tx_power": 14}
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC, connector=dict(stated))])
    endpoint = hub.digital_endpoints[0]
    assert endpoint.sf == 9, "the endpoint carries its own config, not this Hub's"
    hub._mirror_endpoint_config(endpoint, {"sf": 9, "trial": 30})
    hub._probe_visit_end(endpoint, heard=False, completed=False)

    written_before = open(nodes_file).read()
    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert open(nodes_file).read() == written_before


def test_an_entry_written_in_the_legacy_flat_shape_stops_contradicting_itself(tmp_path):
    # The flat sf/bw/cr keys are the older way to state an endpoint's radio, and a connector
    # block wins outright when both are present. Writing the block and leaving the flat keys
    # behind would hand the operator a file that says one thing and a Hub that does another.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC, freq=868, sf=8, bw=125,
                                             cr=1, tx_power=14)])
    endpoint = hub.digital_endpoints[0]
    assert endpoint.sf == 8, "the legacy keys are still read"
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    written = _persisted(nodes_file, "edge")
    assert written["connector"]["sf"] == 9
    assert "sf" not in written and "bw" not in written, "the superseded spelling is gone"
    assert written["asking_frequency"] == 60, "and the keys that are not about RF stay"


def test_the_entry_is_found_by_the_name_the_endpoint_carries_off_the_air(tmp_path):
    # A registered endpoint has no MAC to be found by: v3 never puts one on the wire, so the
    # operator registers a fingerprint and the endpoint is named by device_id[:4]. Matching on
    # mac_address here would find every registered entry at once, all sharing the "no address"
    # default, and write one node's radio into another node's record.
    roster = [_entry("edge-a", device_id="aa11223344556677"),
              _entry("edge-b", device_id="bb8899aabbccddee")]
    for node in roster:
        del node["mac_address"]
    hub, nodes_file = _hub(tmp_path, roster)
    endpoint = [ep for ep in hub.digital_endpoints if ep.get_name() == "edge-b"][0]
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert _persisted(nodes_file, "edge-b")["connector"]["sf"] == 9
    assert "connector" not in _persisted(nodes_file, "edge-a"), \
        "the endpoint that did not move keeps its record"


def test_a_hub_whose_roster_was_built_in_code_persists_nothing(tmp_path):
    # No file was read, so there is none to write back to. Naming one is how such a Hub opts
    # in; every test script and bench script in the tree wants exactly this and nothing more.
    hub_config = str(tmp_path / "hub.json")
    _write_hub_config(hub_config)
    hub = Hub(Loopback_connector(HUB_MAC), config_file=hub_config, nodes_file=None)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True)
    hub.set_digital_endpoints([endpoint])
    _arm(hub, endpoint)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert endpoint.sf == 9, "the change still holds for as long as this Hub runs"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hub.json"], \
        "and nothing was written anywhere"


def test_an_endpoint_with_no_address_is_skipped_and_the_hub_keeps_going(tmp_path):
    # An endpoint registered with neither a MAC nor a device_id has no durable name, so there
    # is nothing to key a record by and nothing a generated one could match after a reboot. It
    # also cannot be polled, so this is a misconfiguration to report rather than to repair.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    nameless = Digital_Endpoint(name="nameless", active=True, session_id=5)
    hub.set_digital_endpoints(hub.digital_endpoints + [nameless])
    before = json.load(open(nodes_file))
    _arm(hub, nameless)

    hub._probe_visit_end(nameless, heard=True, completed=True)

    assert nameless.sf == 9, "the endpoint still holds the config for this Hub's lifetime"
    assert json.load(open(nodes_file)) == before


def test_an_endpoint_whose_entry_is_gone_falls_back_to_the_file(tmp_path):
    # A node registered by MAC and later re-registered by fingerprint changes the name it is
    # known by off the air, which orphans its record. Falling back to what the file says is the
    # safe direction, and it happens at the one moment an operator is editing the file anyway.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)
    with open(nodes_file, "w") as f:
        json.dump([_entry("edge", EDGE_MAC, device_id="cc11223344556677")], f)
    before = json.load(open(nodes_file))

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert json.load(open(nodes_file)) == before, "no entry is claimed by guesswork"


# --- Seam E: the write commits, or it never happened --------------------------------------



def test_the_write_completes_on_a_filesystem_that_will_not_rename_over_a_file(tmp_path,
                                                                             monkeypatch):
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)
    monkeypatch.setattr(node_module, "os", _Fat_os(node_module.os))

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert _persisted(nodes_file, "edge")["connector"]["sf"] == 9
    assert not (tmp_path / "Nodes.json.tmp").exists(), "and the temporary file is not left behind"




def test_a_write_that_dies_part_way_leaves_the_roster_whole(tmp_path, monkeypatch):
    # The reason the write goes through a temporary file: a Hub interrupted while rewriting
    # its roster in place comes back with truncated JSON, which registers NO endpoints at all.
    # Losing the whole fleet is a far worse outcome than the defect this record fixes, so the
    # half-written bytes must land somewhere that is not the file the Hub boots from.
    hub, nodes_file = _hub(tmp_path, [_entry("edge", EDGE_MAC), _entry("gps", "d1d1d1d1")])
    endpoint = hub.digital_endpoints[0]
    _arm(hub, endpoint)
    before = json.load(open(nodes_file))
    monkeypatch.setattr(node_module, "open", _dying_open, raising=False)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    monkeypatch.undo()
    assert json.load(open(nodes_file)) == before, "the roster the Hub boots from is untouched"
    assert len(_reboot(hub, nodes_file).digital_endpoints) == 2, \
        "and a Hub restarted after the failed write still has its fleet"



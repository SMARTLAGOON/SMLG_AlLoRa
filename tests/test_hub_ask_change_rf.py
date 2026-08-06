"""Unit - the Hub's RF-change verb, and where the number it mints with comes from.

`ask_change_rf` is one call that performs a whole radio reconfiguration. It picks a transport
from what the Hub was provisioned with, mints when the Hub holds the authority, and always
attaches the mirror so the Hub follows the Edge onto the new config: a reconfiguration where
only one end moves is not a partial success, it is a deaf endpoint.

The counter those artifacts carry is the Hub's own. One number for the whole fleet, persisted,
and keyed to the root that issued it, because every node compares only against its own mark and
a single increasing sequence satisfies all of them at once. It is never taken from a clock. The
minter may be a board whose RTC does not survive a power cycle, and a clock that guesses high
once would push the number past what any later command can reach, locking the whole fleet out
of its own control plane until the root is rotated and every node re-provisioned.

These tests compose the two halves the way the field does: what the Hub queues, a real node's
verify gate has to accept.
"""
import json

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_types import RF_CONFIG
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.DataSources.DataSource import DataSource
from test_control_root_sink import (
    CONTROL_ROOT_PRIV, OTHER_ROOT_PRIV, TARGET_DEVICE_ID, _CapturingActuator,
)
from test_hub_endpoints import _make_hub, _node

EDGE_MAC = "a1a1a1a1"
NEW_CONFIG = {"sf": 9, "trial": 30}


class _Capturing_downlink(DataSource):
    """Records what the Hub queues on it, and otherwise behaves like the Hub's own FIFO.

    Plugged in through the public `set_downlink_source`, which is the supported way to give an
    endpoint a different outbound boundary, so these tests read the verb's output without
    reaching into the Hub's private queues.
    """

    def __init__(self, chunk_size):
        super().__init__(chunk_size)
        self.queued = []

    def add_to_queue(self, file):
        self.queued.append(file)
        self.file_queue.append(file)


def _hub(tmp_path, **kwargs):
    """A Hub with one device_id-registered endpoint and a capturing downlink on it."""
    hub = _make_hub(tmp_path,
                    nodes=[_node("edge", EDGE_MAC, device_id=TARGET_DEVICE_ID.hex())],
                    **kwargs)
    endpoint = hub.digital_endpoints[0]
    source = _Capturing_downlink(hub.get_chunk_size())
    hub.set_downlink_source(endpoint, source)
    return hub, endpoint, source


def _gate(actuator, root, tmp_path, name="mark.json"):
    """The target node's verify gate, provisioned with the public half of `root`."""
    return Control_Root_DataSink(control_root=root.public_key(), device_id=TARGET_DEVICE_ID,
                                 actuator=actuator, counter_file=str(tmp_path / name))


def _applied_configs(actuator):
    """The RF configs a gate's actuator was handed, decoded back from the artifact payloads."""
    return [json.loads(payload.decode("utf-8"))
            for control_type, payload in actuator.applied if control_type == RF_CONFIG]


def test_a_hub_holding_the_root_queues_an_artifact_the_target_accepts(tmp_path):
    # The deployment with no backend at all: the Hub is its own authority. One call has to
    # produce something a node provisioned with the public half will actually act on, or the
    # self-contained installation cannot reconfigure itself.
    root = Control_Root(CONTROL_ROOT_PRIV)
    hub, endpoint, source = _hub(tmp_path, control_root=root,
                                 control_counter_file=str(tmp_path / "control.counter"))
    actuator = _CapturingActuator()

    hub.ask_change_rf(endpoint, NEW_CONFIG)

    gate = _gate(actuator, root, tmp_path)
    gate.consume(source.queued[0], Reception(source="hub"))
    assert _applied_configs(actuator) == [NEW_CONFIG], \
        "what the Hub mints must be what the target's actuator is handed"


def test_a_second_change_is_not_refused_as_a_replay_of_the_first(tmp_path):
    # The operator's normal life is reconfigure, then reconfigure again. A node keeps the
    # highest counter it has accepted and refuses anything not above it, so a Hub reusing a
    # number would send a perfectly valid artifact that silently does nothing at all.
    root = Control_Root(CONTROL_ROOT_PRIV)
    hub, endpoint, source = _hub(tmp_path, control_root=root,
                                 control_counter_file=str(tmp_path / "control.counter"))
    actuator = _CapturingActuator()
    gate = _gate(actuator, root, tmp_path)

    hub.ask_change_rf(endpoint, {"sf": 9, "trial": 30})
    hub.ask_change_rf(endpoint, {"sf": 10, "trial": 30})

    for queued in source.queued:
        gate.consume(queued, Reception(source="hub"))
    assert [cfg["sf"] for cfg in _applied_configs(actuator)] == [9, 10], \
        "each change must carry a number above the one before it"


def test_a_hub_with_no_root_carries_what_a_backend_minted(tmp_path):
    # The deployment that has a backend: the root private key never goes onto the gateway, and
    # the Hub only carries what someone else signed. It still owns the mirror, because the Hub
    # is the end that has to follow the Edge onto the new configuration either way.
    backend_root = Control_Root(CONTROL_ROOT_PRIV)      # lives at the backend, not on this Hub
    hub, endpoint, source = _hub(tmp_path)
    actuator = _CapturingActuator()
    gate = _gate(actuator, backend_root, tmp_path)

    minted = backend_root.mint(RF_CONFIG, TARGET_DEVICE_ID, 7,
                               json.dumps(NEW_CONFIG).encode("utf-8"))
    hub.ask_change_rf(endpoint, NEW_CONFIG, artifact=minted)

    gate.consume(source.queued[0], Reception(source="hub"))
    assert _applied_configs(actuator) == [NEW_CONFIG], \
        "a Hub holding no authority must deliver a pre-minted artifact untouched"


def test_a_hub_with_neither_a_root_nor_an_artifact_asks_in_band(tmp_path):
    # The third transport: no authority and nothing pre-minted, so the change goes as a request
    # on the link itself, which is what an open-posture deployment has always used. The signed
    # route must not be half-taken here - queueing an unsigned file as a downlink would be a
    # command no gate can accept, delivered as though it were one.
    hub, endpoint, source = _hub(tmp_path)
    hub.send_request = lambda packet: None      # a peer that never answers

    assert hub.ask_change_rf(endpoint, {"sf": 9}) is False, \
        "the in-band exchange reports whether it landed"
    assert source.queued == [], "the in-band route must queue no downlink"


def test_a_restarted_hub_does_not_reissue_numbers_the_fleet_has_seen(tmp_path):
    # A Hub reboots, or is power-cycled in the field. If its counter began again, every artifact
    # it minted afterwards would carry a number its nodes had already accepted: delivered,
    # verified, and then dropped as a replay, with nothing on either side saying why. The mark
    # on the node survives a reboot on purpose, so the minter's number has to survive one too.
    root = Control_Root(CONTROL_ROOT_PRIV)
    counter_file = str(tmp_path / "control.counter")
    actuator = _CapturingActuator()
    gate = _gate(actuator, root, tmp_path)

    hub, endpoint, source = _hub(tmp_path, control_root=root, control_counter_file=counter_file)
    hub.ask_change_rf(endpoint, {"sf": 9, "trial": 30})
    gate.consume(source.queued[0], Reception(source="hub"))

    restarted, endpoint, source = _hub(tmp_path, control_root=root,
                                       control_counter_file=counter_file)
    restarted.ask_change_rf(endpoint, {"sf": 10, "trial": 30})
    gate.consume(source.queued[0], Reception(source="hub"))

    assert [cfg["sf"] for cfg in _applied_configs(actuator)] == [9, 10], \
        "a restarted Hub must keep issuing numbers above what its fleet has already accepted"

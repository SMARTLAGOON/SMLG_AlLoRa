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

The call answers with one of four words, and half these tests are about which one. Delivering a
command is not the same as having it taken, and a peer that says no is not a peer that is not
there: a boolean fused both pairs, so a refusal was reported as a success on one route and as a
dead antenna on the other. The Hub now reports what it can observe and says `pending` for what
it cannot yet, and the {new, old} probe is what turns that into a verdict.

These tests compose the two halves the way the field does: what the Hub queues, a real node's
verify gate has to accept.
"""
import json

from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_types import RF_CONFIG
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Nodes.Node import Node
from AlLoRa.Packet_v3 import Packet_v3
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

    def __init__(self):
        super().__init__()
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
    source = _Capturing_downlink()
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

    assert hub.ask_change_rf(endpoint, {"sf": 9}) == Hub.UNREACHABLE, \
        "the in-band exchange reports whether it landed"
    assert source.queued == [], "the in-band route must queue no downlink"


def test_the_in_band_route_speaks_the_control_frame_a_v3_peer_listens_for(tmp_path):
    # What the third transport actually puts on the air. It used to fall through to the v2
    # verb, which builds a legacy flag packet unconditionally; a v3 node ignores that flag
    # outright, so an open v3 deployment had no working way to retune at all. The change went
    # out, was dropped in silence, and the call reported a bad link after 20 attempts rather
    # than a missing feature.
    hub, endpoint, source = _hub(tmp_path)
    sent = []
    hub.send_request = lambda packet: sent.append(packet)   # capture, answer nothing

    hub.ask_change_rf(endpoint, NEW_CONFIG)

    assert sent, "an open Hub must put the change on the air"
    assert sent[0].get_command() == Packet_v3.CTRL, "an in-band command rides a CTRL frame"
    payload = sent[0].get_payload()
    assert payload[0] == 0x80 | RF_CONFIG, \
        "the prefix is the namespace bit plus the control type verbatim"
    assert json.loads(payload[1:].decode("utf-8")) == NEW_CONFIG, \
        "the body is the same JSON the signed envelope carries, so both transports reach the "\
        "actuator with identical arguments"


def test_the_legacy_verb_refuses_on_a_v3_node(tmp_path):
    # The same defect the dispatch above fixes, in its general form. `ask_change_rf` lives on
    # Node, so the legacy implementation is reachable from any node, and it builds a v2 flag
    # packet unconditionally. A v3 node ignores that flag on receipt, so an unguarded call
    # transmits twenty frames nobody listens to and then reports a bad link, which sends
    # whoever is debugging it to the antenna instead of to the version mismatch.
    #
    # Called unbound on purpose: the subject is the legacy implementation itself, not which
    # transport the Hub's override would have selected.
    hub, endpoint, source = _hub(tmp_path)
    sent = []
    hub.send_request = lambda packet: sent.append(packet)

    assert Node.ask_change_rf(hub, endpoint, {"sf": 9}) is False, \
        "the legacy verb has no transport on a v3 link and must say so"
    assert sent == [], "a v3 node must put no v2 flag frame on the air"


def test_an_echoed_prefix_is_the_acknowledgement(tmp_path):
    # What lets the call report "applied" rather than "queued". The ack is the prefix alone,
    # no body: the Hub already holds the config it asked for, so echoing it back would buy
    # nothing and cost airtime, and the type acked is proof the peer parsed the type sent.
    hub, endpoint, source = _hub(tmp_path)
    sent = []

    def _echo(packet):
        sent.append(packet)
        reply = hub.new_packet()
        reply.set_kind(Packet_v3.CTRL)
        reply.set_payload(bytes([0x80 | RF_CONFIG]))
        return reply

    hub.send_request = _echo

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.ACCEPTED, \
        "an echoed prefix is the peer accepting the command"
    assert len(sent) == 1, "an acknowledged command must not be sent again"


def test_an_ordinary_keepalive_is_not_mistaken_for_an_acknowledgement(tmp_path):
    # Why the ack is a CTRL frame carrying the prefix rather than a plain OK. OK is also the
    # connection-poll and keepalive kind, so a peer that is merely alive would otherwise read
    # as a peer that accepted the change: the Hub would report success, stop retrying, and
    # move itself onto a configuration the Edge never applied. That is the deaf-endpoint
    # failure, reached by believing an ack that was never sent.
    #
    # A peer this shape is alive and did not accept, so the report is a refusal rather than a
    # link problem. That is the whole point of asking the poll: the outcome follows what the
    # peer did, not what the Hub failed to hear.
    hub, endpoint, source = _hub(tmp_path)

    def _keepalive(packet):
        reply = hub.new_packet()
        reply.set_ok()
        return reply

    hub.send_request = _keepalive

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.REFUSED, \
        "only a control ack acknowledges a control command, and a peer that answers is not gone"


def test_an_edge_that_heard_the_command_and_declined_is_reported_as_a_refusal(tmp_path):
    # The defect. A node holding a control root refuses an unsigned command and answers nothing,
    # which is correct on the wire: acknowledging would tell the Hub a change happened that did
    # not. But silence is also what a broken antenna produces, so the two outcomes arrived at the
    # operator as one, and the repair a Hub that needs the signing key was reported as a site
    # visit. That is the normal state of a half-provisioned rollout, not an edge case.
    #
    # The disambiguator is the one question an Edge always answers. Answered, on the very config
    # the command went out on, means it heard the command and chose not to acknowledge it.
    hub, endpoint, source = _hub(tmp_path)

    def _declines_control_but_is_alive(packet):
        if packet.get_command() == Packet_v3.CTRL:
            return None             # heard it, refused it, said nothing: the provisioned Edge
        reply = hub.new_packet()
        reply.set_ok()
        return reply

    hub.send_request = _declines_control_but_is_alive

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.REFUSED, \
        "a peer that answers the connection poll heard the command and declined it"


def test_a_queued_artifact_is_pending_and_not_a_success(tmp_path):
    # The other direction of the same defect. On the signed route all this Hub can observe is
    # that a file was handed over, and the Edge reaches its verdict afterwards, alone, with the
    # transaction already closed. So "delivered" was being reported as "applied", which is a
    # prediction dressed as an observation, and it was wrong exactly when a node refused the
    # artifact as a replay on a stale counter.
    #
    # At the moment of queuing the honest answer is that nothing has happened yet. The probe
    # settles it later.
    root = Control_Root(CONTROL_ROOT_PRIV)
    hub, endpoint, source = _hub(tmp_path, control_root=root,
                                 control_counter_file=str(tmp_path / "control.counter"))

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.PENDING, \
        "queuing an artifact is not the Edge taking it"
    assert hub.rf_change_status(endpoint) == Hub.PENDING, \
        "and the outstanding change stays readable, because the verdict arrives later"


def _pending_signed_change(tmp_path):
    """A Hub that minted a change, saw it delivered, and is now probing for the verdict.

    The two steps after the call are what the drive loop does when an Edge's pull completes:
    mirror this end onto the new config, then start probing {new, old}. The first probe visit
    is consumed here because the mirror happened part way through a visit that polled entirely
    on the old config, so it never actually probed anything.
    """
    root = Control_Root(CONTROL_ROOT_PRIV)
    hub, endpoint, source = _hub(tmp_path, control_root=root,
                                 control_counter_file=str(tmp_path / "control.counter"),
                                 probe_swap_after=1, probe_give_up_after=4)
    hub.ask_change_rf(endpoint, NEW_CONFIG)
    hub._mirror_endpoint_config(endpoint, NEW_CONFIG)
    hub._probe_visit_end(endpoint, heard=False, completed=False)
    assert hub.rf_change_status(endpoint) == Hub.PENDING, "the verdict is not in yet"
    return hub, endpoint


def test_the_probe_settles_a_pending_change_as_accepted_when_the_edge_took_it(tmp_path):
    # The verdict this Hub could never see before. A full exchange on the new configuration is
    # the Edge running on it, which is the only proof that exists: the Edge's own acceptance
    # happened alone, after the transfer closed, with nothing on the air to carry it back.
    hub, endpoint = _pending_signed_change(tmp_path)

    hub._probe_visit_end(endpoint, heard=True, completed=True)

    assert hub.rf_change_status(endpoint) == Hub.ACCEPTED, \
        "an endpoint working on the new configuration took the change"


def test_the_probe_settles_a_pending_change_as_refused_when_the_edge_never_moved(tmp_path):
    # What happened on the bench on 2026-08-07, reported as a success. The Hub queued an
    # artifact, the pull completed, the Hub moved its own radio and called it done. The Edge
    # then verified the artifact and refused it as a replay on a stale counter, which was the
    # correct thing for it to do. The two ends were split, and the pair only came back because
    # the probe happened to find the Edge on the configuration it had never left.
    #
    # Finding it there is not luck, it is the evidence. An endpoint answering on the old
    # configuration after a delivery that completed did not take the change.
    hub, endpoint = _pending_signed_change(tmp_path)

    hub._probe_visit_end(endpoint, heard=False, completed=False)   # silence: swap to old
    hub._probe_visit_end(endpoint, heard=True, completed=False)    # and there it is

    assert endpoint.sf == 7, "the endpoint is back where it started"
    assert hub.rf_change_status(endpoint) == Hub.REFUSED, \
        "an endpoint found on the old configuration after a completed delivery did not take it"


def test_a_change_the_probe_never_locates_is_a_link_problem_not_a_refusal(tmp_path):
    # The probe can also just run out. It looked on both configurations, found the endpoint on
    # neither, and restored the old one as the best chance of re-contact. Nothing there says
    # the Edge refused anything, so nothing here may claim it did: the same rule the in-band
    # route follows, that an outcome nobody answered for is a link report.
    hub, endpoint = _pending_signed_change(tmp_path)

    for _ in range(4):                          # probe_give_up_after=4
        hub._probe_visit_end(endpoint, heard=False, completed=False)

    assert hub.endpoint_trial_old(endpoint) is None, "the probe gave up"
    assert hub.rf_change_status(endpoint) == Hub.UNREACHABLE, \
        "an endpoint located on neither configuration is a link report"


def test_an_acknowledged_in_band_change_is_not_rewritten_by_its_own_trial(tmp_path):
    # Where the probe has to keep quiet. In band the peer answered for itself: it acknowledged
    # the command and applied it, so the change WAS accepted, and no later observation makes
    # that untrue. An Edge whose trial then reverts is the trial doing its job on a
    # configuration that could not carry a transfer, which is a different event with a
    # different repair. Rewriting the verdict to "refused" would send whoever reads it to
    # re-provision a node whose provisioning was never the problem.
    hub, endpoint, source = _hub(tmp_path, probe_swap_after=1, probe_give_up_after=4)

    def _echo(packet):
        reply = hub.new_packet()
        reply.set_kind(Packet_v3.CTRL)
        reply.set_payload(bytes([0x80 | RF_CONFIG]))
        return reply

    hub.send_request = _echo
    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.ACCEPTED

    hub._probe_visit_end(endpoint, heard=False, completed=False)   # silence: swap to old
    hub._probe_visit_end(endpoint, heard=True, completed=False)    # the Edge self-restored

    assert endpoint.sf == 7, "the Edge rolled back and the Hub followed it"
    assert hub.rf_change_status(endpoint) == Hub.ACCEPTED, \
        "a command the peer acknowledged stays accepted, whatever its trial later decided"


def test_one_lost_poll_does_not_turn_a_refusal_back_into_a_link_report(tmp_path):
    # The diagnosis rests on a single short frame, so it has to survive the link losing one.
    # The twenty unanswered commands say nothing about the return path: a node that refuses
    # hears every one of them and simply does not reply, so the first frame that actually
    # tests whether anything can come back is the poll. Betting the whole diagnosis on one
    # frame over a lossy radio would put the old wrong answer back for free.
    hub, endpoint, source = _hub(tmp_path)
    polls = []

    def _drops_the_first_poll(packet):
        if packet.get_command() == Packet_v3.CTRL:
            return None                     # the refusal: heard, declined, silent
        polls.append(packet)
        if len(polls) == 1:
            return None                     # and the radio eats the first poll
        reply = hub.new_packet()
        reply.set_ok()
        return reply

    hub.send_request = _drops_the_first_poll

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.REFUSED, \
        "a peer that answers the second poll was there for the first one too"


def test_a_peer_that_answers_nothing_at_all_is_reported_as_a_link_problem(tmp_path):
    # The other half of the same branch, and the half that keeps the fix honest. Diagnosing a
    # refusal is only worth anything if a broken antenna is still diagnosed as a broken antenna:
    # a Hub that reported every silence as a refusal would have swapped one wrong answer for
    # another, and sent whoever is on call to re-provision a node that is off the air.
    #
    # An unanswered poll is also the case where the Edge heard the command, applied it, and had
    # its acknowledgement lost, so it is now on a configuration this Hub cannot hear. That is
    # the same report and the same repair, which is why one value covers both.
    hub, endpoint, source = _hub(tmp_path)
    hub.send_request = lambda packet: None      # a peer that answers nothing, command or poll

    assert hub.ask_change_rf(endpoint, NEW_CONFIG) == Hub.UNREACHABLE, \
        "silence to the poll as well as to the command is a link report, never a refusal"


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

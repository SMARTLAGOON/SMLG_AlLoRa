"""The in-band control transport, receive side: a control command carried by the link itself.

An open deployment has no control root and no signed artifact, so there is nothing to verify
and nothing to wrap the command in: the frame *is* the command. It rides a CTRL packet whose
1-byte payload prefix is the in-band namespace bit ORed with the control type, and the rest of
the payload is the same JSON a signed envelope carries. Both transports therefore reach the
node's actuator with identical arguments, and the actuator never learns which one delivered it.

Two things here are load-bearing beyond the happy path:

  * **The namespace bit gives an unknown frame somewhere to belong.** Handshake kinds occupy
    0x00 to 0x7F and control types 0x81 upward, so an unrecognised prefix can be dropped *as*
    the thing it claimed to be. Before the split, an unknown handshake kind and an unknown
    control type were the same silent `None` and neither could be logged for what it was.
  * **A Hub does not act on one.** Actuation follows authority: a node that is an authority
    evaluates control from below rather than applying it. The blast radius is the reason. A
    wrongly retuned Edge costs that node until its trial reverts it; a wrongly retuned Hub
    moves the aggregation point for every Edge at once, and it is the one node whose recovery
    is nobody else's problem.
  * **A node provisioned with a control root does not act on one either.** Provisioning is the
    operator declaring an external authority, so the stronger tier becomes the only tier that
    node accepts. If an unsigned frame could still retune it, the signed path would secure
    nothing: anyone in radio range could simply ask in band instead.
"""
import json
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.Control.control_types import IN_BAND, RF_CONFIG, RESET
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.ec_p256 import public_key_uncompressed

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
NEW_CONFIG = {"sf": 9, "trial": 30}
RF_BODY = json.dumps(NEW_CONFIG).encode("utf-8")

# The verifying half an operator provisions onto a commanded node, derived from RFC 6979
# A.2.5's published test scalar so it is unmistakably not a real key.
CONTROL_ROOT_PUB = public_key_uncompressed(
    0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721)


class _CapturingActuator:
    """Records what it was asked to do, so a test can compare the in-band path's arguments
    against the signed path's without a radio or a real reconfiguration."""

    def __init__(self):
        self.applied = []

    def apply(self, control_type, payload):
        self.applied.append((control_type, payload))


def _write_config(path, name="edge"):
    config = {
        "name": name, "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_edge(tmp_path, **kwargs):
    path = str(tmp_path / "edge.json")
    _write_config(path)
    conn, peer = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    edge = Edge(conn, config_file=path, **kwargs)
    return edge, peer


def _control_frame(edge, prefix, body=b""):
    """The frame a Hub's in-band verb puts on the air, built the way the wire carries it."""
    packet = Packet_v3(addressing="sid")
    packet.set_session(SESSION_ID)
    packet.set_kind(Packet_v3.CTRL)
    packet.set_payload(bytes([prefix]) + body)
    return edge.connector.codec.frame(packet)


def _replies(peer, edge):
    """Whatever the node under test transmitted, parsed back off the peer's inbox."""
    out = []
    while not peer.inbox.empty():
        out.append(edge.connector.codec.deframe(peer.inbox.get()))
    return out


# --- the command reaches the actuator, with the signed path's arguments ----------------------

def test_an_in_band_command_reaches_the_actuator_with_the_signed_paths_arguments(tmp_path):
    # The whole point of the prefix encoding. If the in-band route handed the actuator anything
    # other than (control_type, the same JSON), the actuator would need to know which transport
    # delivered it, and every control type would then need implementing twice.
    actuator = _CapturingActuator()
    edge, peer = _make_edge(tmp_path, control_actuator=actuator)
    edge.connector.inbox.put(_control_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))

    edge.respond(edge._respond_handler)

    assert actuator.applied == [(RF_CONFIG, RF_BODY)], \
        "the type is the prefix's low bits and the payload is the body, verbatim"


def test_the_command_is_acknowledged_by_echoing_its_prefix(tmp_path):
    # The ack the drive side waits on. It carries the prefix and no body: the Hub already holds
    # the config it asked for, so echoing it back would only cost airtime, and the type acked
    # is what proves the peer parsed the type that was sent.
    actuator = _CapturingActuator()
    edge, peer = _make_edge(tmp_path, control_actuator=actuator)
    edge.connector.inbox.put(_control_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))

    edge.respond(edge._respond_handler)

    replies = _replies(peer, edge)
    assert len(replies) == 1, "an in-band command is answered exactly once"
    assert replies[0].get_command() == Packet_v3.CTRL, "the ack rides a CTRL frame"
    assert replies[0].get_payload() == bytes([IN_BAND | RF_CONFIG]), \
        "the ack is the prefix alone"


def test_a_reset_command_travels_the_same_path(tmp_path):
    # The vocabulary is the closed control-type enum, not an RF-specific verb, so a second type
    # needs no second encoding. This is what the prefix buys over a bespoke RF frame.
    actuator = _CapturingActuator()
    edge, peer = _make_edge(tmp_path, control_actuator=actuator)
    edge.connector.inbox.put(_control_frame(edge, IN_BAND | RESET))

    edge.respond(edge._respond_handler)

    assert actuator.applied == [(RESET, b"")], "any control type rides the same prefix"


# --- an unknown prefix is dropped, and dropped as the thing it claimed to be -----------------

def test_an_unknown_control_type_is_dropped_without_a_reply(tmp_path):
    # A control type this build does not implement. Dropping it is right; answering it would
    # tell a caller the command was accepted. The namespace bit is what makes this a *control*
    # drop rather than an indistinguishable silent None shared with the handshake.
    #
    # This also keeps the door open for a later suggestive-mood bit: a proposal frame would
    # arrive with bit 6 set, read here as a control type well outside the closed enum, and be
    # dropped rather than mistaken for a command.
    actuator = _CapturingActuator()
    edge, peer = _make_edge(tmp_path, control_actuator=actuator)
    edge.connector.inbox.put(_control_frame(edge, IN_BAND | 0x40 | RF_CONFIG, RF_BODY))

    edge.respond(edge._respond_handler)

    assert actuator.applied == [], "an unknown control type must not reach the actuator"
    assert _replies(peer, edge) == [], "and must not be acknowledged"


def test_a_node_with_no_actuator_drops_the_command(tmp_path):
    # An open node that was never given an actuator has nothing to act with. It must not
    # acknowledge: an ack would move the Hub onto a config this node is not going to apply,
    # which is the deaf-endpoint failure the mirror exists to prevent.
    edge, peer = _make_edge(tmp_path)
    edge.connector.inbox.put(_control_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))

    edge.respond(edge._respond_handler)

    assert _replies(peer, edge) == [], "nothing to act with is not an acceptance"


# --- a provisioned node accepts only the tier it was provisioned for -------------------------

def test_a_node_holding_a_control_root_refuses_an_in_band_command(tmp_path):
    # The security-critical one. Provisioning a control root is the operator saying an external
    # authority commands this node; if an unsigned in-band frame could still retune it, the
    # signed path would secure nothing, because an attacker would simply not use it. So the
    # tier is a property of the node, not of the frame: once provisioned, the stronger tier is
    # the only one this node accepts, and the refusal is unconditional rather than configurable.
    actuator = _CapturingActuator()
    edge, peer = _make_edge(tmp_path, control_actuator=actuator)
    edge.control_root = CONTROL_ROOT_PUB

    edge.connector.inbox.put(_control_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))
    edge.respond(edge._respond_handler)

    assert actuator.applied == [], \
        "a node that verifies signed control must never act on an unsigned command"
    assert _replies(peer, edge) == [], \
        "and must not acknowledge one: an ack would report a change that never happened"


# --- actuation follows authority ------------------------------------------------------------

def test_a_hub_does_not_act_on_an_in_band_command(tmp_path):
    # The authority does not take orders on the link it commands. An Edge holding the collector
    # role during a downlink delegation is mechanically able to drive a control round, since
    # driving is what retunes RF, but the delegation is scoped to the pull it was granted and
    # carries no authority with it. Without this, anything in radio range could retune the one
    # node the whole fleet is aimed at.
    path = str(tmp_path / "hub.json")
    _write_config(path, name="hub")
    conn, peer = Loopback_connector.create_pair(HUB_MAC, EDGE_MAC)
    actuator = _CapturingActuator()
    hub = Hub(conn, config_file=path, control_actuator=actuator)
    hub.set_digital_endpoints([Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                                session_id=SESSION_ID)])
    hub.connector.inbox.put(_control_frame(hub, IN_BAND | RF_CONFIG, RF_BODY))

    hub.respond(hub._respond_handler)

    assert actuator.applied == [], "a Hub must not actuate control that arrived in band"
    assert _replies(peer, hub) == [], "and must not acknowledge it as accepted"


# --- the two halves, over a real link -------------------------------------------------------

def test_a_hub_retunes_an_edge_over_the_link_itself(tmp_path):
    # Both ends over one connector pair, which is the only check that pins the encoding: each
    # side's own unit tests would keep passing if the two disagreed about the prefix. It also
    # exercises what a bench run does, minus the radio: the Hub asks, the Edge acknowledges on
    # the old configuration, applies afterwards, and comes up on the new one with a trial armed
    # so a config it cannot be reached on reverts instead of stranding it.
    edge_path = str(tmp_path / "edge.json")
    hub_path = str(tmp_path / "hub.json")
    _write_config(edge_path)
    _write_config(hub_path, name="hub")
    edge_conn, hub_conn = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)

    edge = Edge(edge_conn, config_file=edge_path)
    # The actuator needs the node it acts on, so it is wired after construction: the same
    # ordering the signed route has, where the verify gate wrapping an actuator is built once
    # the node exists.
    edge.control_actuator = Node_Control_Actuator(edge)

    hub = Hub(hub_conn, config_file=hub_path)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                session_id=SESSION_ID)
    hub.set_digital_endpoints([endpoint])

    stop = threading.Event()

    def serve():
        while not stop.is_set():
            edge.respond(edge._respond_handler)

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    try:
        assert hub.ask_change_rf(endpoint, NEW_CONFIG) is True, \
            "the Edge acknowledged, so the exchange landed"
    finally:
        stop.set()
        server.join(timeout=5)

    assert edge.connector.sf == NEW_CONFIG["sf"], \
        "the Edge must end up on the configuration it acknowledged"
    assert edge.sf_trial, \
        "and hold it on trial: an in-band change is no more proven than a signed one, so it "\
        "reverts if the link does not come back"
    # The Hub follows by moving its *view* of the endpoint; prepare_connector tunes the radio
    # to those fields at the next visit. Asserting on the endpoint rather than on the Hub's
    # connector is therefore the real check: a reconfiguration where only one end moves is not
    # a partial success, it is an endpoint this Hub can no longer hear.
    assert endpoint.sf == NEW_CONFIG["sf"], "the Hub must follow the Edge onto the new config"
    assert hub.endpoint_trial_old(endpoint)[1] == 7, \
        "and retain the old one as the probe fallback, so a change that silences the link "\
        "has somewhere concrete to roll back to"

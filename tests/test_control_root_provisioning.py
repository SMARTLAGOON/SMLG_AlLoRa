"""Provisioning a control root from the config file, and the posture that follows from it.

`LoRa.json` carries one key, `control_root_file`, with the same name and the same meaning on
both node kinds, exactly as `identity_file` already does. It holds a path and never key
material. What the file contains decides the role, unambiguously by length: a 130-hex SEC1
public key is the verifying half an Edge is commanded with, a 64-hex P-256 scalar is the
signing half a Hub mints with.

Why this lives in the config rather than in a conventional filename picked up if present:
holding a control root changes what the node accepts, since a provisioned node refuses
unsigned in-band commands. A posture that does not appear in `LoRa.json` cannot be read off a
node's configuration, and a leftover key file from an earlier provisioning would silently
re-arm that refusal on a reflashed board.

Every misprovisioning here halts at construction rather than degrading. The precedent is
`security_mode 'strict'`, refused at startup because a posture that silently delivers less
than it names is worse than no posture at all. The same argument applies twice over on this
surface: a node that quietly comes up with no control root accepts commands nobody signed.
"""
import json
import os
import subprocess
import sys

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Control.Control_Root import Control_Root
from AlLoRa.Control.control_envelope import TARGET_LEN
from AlLoRa.Control.control_types import IN_BAND, RF_CONFIG
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.Security.ec_p256 import public_key_uncompressed
from test_hub_ask_change_rf import _Capturing_downlink

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
NEW_CONFIG = {"sf": 9, "trial": 30}
RF_BODY = json.dumps(NEW_CONFIG).encode("utf-8")

# RFC 6979 A.2.5's published test scalar, used here because it is unmistakably not a real key.
ROOT_PRIV_HEX = "c9afa9d845ba75166b5c215767b1d6934e50c3db36e89b127b8a622b120f6721"
ROOT_PUB_HEX = public_key_uncompressed(int(ROOT_PRIV_HEX, 16)).hex()
DEVICE_ID = bytes(range(32))


class _CapturingActuator:
    def __init__(self):
        self.applied = []

    def apply(self, control_type, payload):
        self.applied.append((control_type, bytes(payload)))


def _key_file(tmp_path, contents, name="control_root.key"):
    path = tmp_path / name
    with open(str(path), "w") as f:
        f.write(contents)
    return str(path)


def _config(tmp_path, name="edge", **extra):
    """A v3 open config, plus whatever provisioning the test is exercising."""
    path = str(tmp_path / "{}.json".format(name))
    config = {
        "name": name, "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config.update(extra)
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def _edge(tmp_path, **extra):
    conn, peer = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=_config(tmp_path, **extra)), peer


def _hub(tmp_path, **extra):
    conn, peer = Loopback_connector.create_pair(HUB_MAC, EDGE_MAC)
    hub = Hub(conn, config_file=_config(tmp_path, name="hub", **extra), nodes_file=None)
    return hub, peer


def _registered_endpoint(hub):
    """One device_id-registered endpoint on `hub`, with its downlink captured so a test can
    read back what the verb queued without reaching into the Hub's private queues."""
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True,
                                device_id=DEVICE_ID.hex())
    hub.set_digital_endpoints([endpoint])
    hub.set_downlink_source(endpoint, _Capturing_downlink(hub.get_chunk_size()))
    return endpoint


def _minted_counter(hub):
    """The counter carried by the last artifact this Hub queued. The envelope's signed region
    is version, type, the 32-byte target, then the counter, so it reads back off the bytes
    without a key: the number is authenticated, not secret."""
    artifact = hub._downlink[hub.digital_endpoints[0].session_id].queued[-1].get_content()
    return int.from_bytes(artifact[2 + TARGET_LEN:2 + TARGET_LEN + 4], "big")


def _in_band_frame(node, prefix, body=b""):
    packet = Packet_v3(addressing="sid")
    packet.set_session(SESSION_ID)
    packet.set_kind(Packet_v3.CTRL)
    packet.set_payload(bytes([prefix]) + body)
    return node.connector.codec.frame(packet)


# --- an Edge is provisioned with the verifying half ------------------------------------------

def test_a_configured_public_key_puts_an_edge_in_the_signed_posture(tmp_path):
    # The point of the config key. An operator who provisions a control root has declared an
    # external authority over this node, and from that moment the node accepts only what that
    # authority signed. Asserted through behavior rather than through the attribute, because
    # the refusal is the posture: an Edge that loads a key and still takes unsigned commands
    # is provisioned in name only.
    actuator = _CapturingActuator()
    edge, peer = _edge(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PUB_HEX))
    edge.control_actuator = actuator

    edge.connector.inbox.put(_in_band_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))
    edge.respond(edge._respond_handler)

    assert actuator.applied == [], \
        "a node provisioned from config must refuse an unsigned command like any other"


def test_the_loaded_key_is_what_the_verify_gate_pins(tmp_path):
    # The other half of provisioning: the same value has to be usable by the gate that checks
    # signatures, or the config key would declare a posture the node cannot actually carry out.
    # Constructing the gate from it is the check, since the gate validates the curve point and
    # refuses anything malformed at construction.
    edge, _ = _edge(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PUB_HEX))

    gate = Control_Root_DataSink(control_root=edge.control_root, device_id=DEVICE_ID,
                                 actuator=_CapturingActuator())

    assert gate.control_root == bytes.fromhex(ROOT_PUB_HEX), \
        "the provisioned key must reach the gate as the operator wrote it"


def test_an_edge_handed_the_private_scalar_halts(tmp_path):
    # The dangerous misprovisioning: the key every command in the fleet is signed with, sitting
    # on a node in the field. Deriving the public half from it and carrying on would leave a
    # working link with a compromised root behind it, so the node refuses to start at all.
    with pytest.raises(ValueError) as excinfo:
        _edge(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX))

    assert "private scalar" in str(excinfo.value), \
        "the error has to name what was found, or the operator cannot tell which half to fix"


# --- a Hub is provisioned with the signing half ----------------------------------------------

def test_a_configured_private_scalar_lets_a_hub_mint(tmp_path):
    # The self-contained installation: no backend anywhere, the Hub is its own authority, and
    # the only thing that made it one is a line in its config. What it mints has to verify
    # against the public half its Edges carry, which is the whole round trip in one assertion.
    hub, _ = _hub(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX))

    assert isinstance(hub.control_root, Control_Root), \
        "a Hub provisioned with the signing half must come up able to mint"
    assert hub.control_root.public_key_hex() == ROOT_PUB_HEX, \
        "and mint under the same root its Edges are provisioned to verify"


def test_the_mint_counter_file_is_configured_beside_the_root(tmp_path):
    # The counter belongs to the same provisioning act as the key that signs with it: a Hub
    # that mints and does not remember what it has minted re-uses a number, and every target
    # refuses that artifact as a replay. Nothing on either side says why, so the reconfiguration
    # simply never lands. Naming the file in config is what makes surviving a reboot the
    # default rather than something a deployment has to remember to wire up in code.
    hub, _ = _hub(tmp_path,
                  control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX),
                  control_counter_file=str(tmp_path / "control.counter"))
    endpoint = _registered_endpoint(hub)
    hub.ask_change_rf(endpoint, NEW_CONFIG)

    rebooted, _ = _hub(tmp_path,
                       control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX),
                       control_counter_file=str(tmp_path / "control.counter"))
    rebooted.ask_change_rf(_registered_endpoint(rebooted), NEW_CONFIG)

    assert _minted_counter(rebooted) > _minted_counter(hub), \
        "a Hub restarted on the same config must carry on above the number it left off at"


def test_a_hub_that_mints_keeps_its_number_with_no_counter_file_named(tmp_path, monkeypatch):
    # The deployment nobody configured, which is every operator who provisions a root by
    # following the example. A node keeps the highest number it has accepted whether or not it
    # was told to, so the minter has to keep its own the same way: a Hub that starts its
    # sequence over is refused by every node it commands, for good, because it can only work
    # back through the numbers it already spent one restart at a time. Recovery is rotating the
    # root across the fleet. The two ends default together or the pair is broken by its
    # defaults alone, with nothing misconfigured anywhere.
    monkeypatch.chdir(tmp_path)
    hub, _ = _hub(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX))
    hub.ask_change_rf(_registered_endpoint(hub), NEW_CONFIG)

    rebooted, _ = _hub(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PRIV_HEX))
    rebooted.ask_change_rf(_registered_endpoint(rebooted), NEW_CONFIG)

    assert _minted_counter(rebooted) > _minted_counter(hub), \
        "a Hub nobody named a counter file for must still not re-issue a number its fleet took"


def test_a_hub_with_nothing_to_mint_under_leaves_no_counter_behind(tmp_path, monkeypatch):
    # The other half of that default: it has to be inert where it is not needed. Most
    # deployments provision no root at all, and a counter written on their behalf would put a
    # file nobody asked for in the working directory of every one of them.
    monkeypatch.chdir(tmp_path)

    hub, _ = _hub(tmp_path)

    assert hub.control_root is None
    assert not os.path.exists("control.counter"), \
        "a Hub with no authority to mint under has no number to keep"


def test_a_hub_handed_only_the_public_key_halts(tmp_path):
    # A Hub cannot sign with a verifying key, and it never verifies anything itself: over the
    # radio it commands, it is not commanded. So this file has no role on this node, and it is
    # almost always the wrong half of the pair copied in. Starting anyway would leave a Hub
    # whose config claims a signed deployment quietly sending unsigned commands instead.
    with pytest.raises(ValueError) as excinfo:
        _hub(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PUB_HEX))

    assert "public half" in str(excinfo.value)


# --- a declared posture that cannot be loaded is never a silent open node --------------------

def test_a_missing_key_file_halts_rather_than_coming_up_unprovisioned(tmp_path):
    # The reflashed board, or the key that never got copied across. Coming up without a control
    # root would leave a node that accepts unsigned commands while its own config says it does
    # not, and nothing on the wire would look wrong until someone retuned it.
    with pytest.raises(ValueError) as excinfo:
        _edge(tmp_path, control_root_file=str(tmp_path / "never_provisioned.key"))

    assert "could not be read" in str(excinfo.value)


def test_a_file_of_the_wrong_length_halts(tmp_path):
    # Truncated, or a pasted fragment. Length is the only thing telling the two halves apart,
    # so anything else cannot be classified and must not be guessed at.
    with pytest.raises(ValueError) as excinfo:
        _edge(tmp_path, control_root_file=_key_file(tmp_path, ROOT_PUB_HEX[:100]))

    assert "100 characters" in str(excinfo.value), \
        "naming the length found is what makes a truncated paste obvious"


def test_a_key_of_the_right_length_that_is_not_a_curve_point_halts(tmp_path):
    # Right shape, wrong contents: 130 hex characters that do not describe a point on P-256.
    # Signature verification against it would fail every time, which in the field reads as a
    # backend minting bad artifacts rather than as a mis-copied key.
    off_curve = "04" + "11" * 64
    with pytest.raises(ValueError):
        _edge(tmp_path, control_root_file=_key_file(tmp_path, off_curve))


# --- the tool that writes the files, and the nodes that read them ----------------------------

def test_the_provisioning_tool_writes_what_the_nodes_load(tmp_path):
    # The gap this closes is the one nobody notices until a board is in the field: the library
    # can generate a root and the nodes can load one, but nothing checked that what the
    # operator's tool writes is what a node accepts. Run end to end, as an operator runs it.
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "examples", "v3_hello", "provision_control_root.py")
    subprocess.check_call([sys.executable, script, str(tmp_path)])

    hub, _ = _hub(tmp_path, control_root_file=str(tmp_path / "hub" / "control_root.key"))
    edge, _ = _edge(tmp_path, control_root_file=str(tmp_path / "edge" / "control_root.key"))

    assert isinstance(hub.control_root, Control_Root), "the Hub half has to mint"
    assert hub.control_root.public_key() == edge.control_root, \
        "and the Edge half has to be the matching verifying key, or nothing the Hub signs "\
        "will ever be accepted by the node it was signed for"


def test_re_provisioning_never_replaces_a_live_root(tmp_path):
    # Adding a node to an existing fleet runs the same tool again. If that minted a fresh root,
    # every board already deployed would be pinned to the old one and would refuse every
    # command from then on, recoverable only by re-provisioning each node by hand.
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "examples", "v3_hello", "provision_control_root.py")
    subprocess.check_call([sys.executable, script, str(tmp_path)])
    with open(str(tmp_path / "hub" / "control_root.key")) as f:
        first = f.read()

    subprocess.check_call([sys.executable, script, str(tmp_path)])

    with open(str(tmp_path / "hub" / "control_root.key")) as f:
        assert f.read() == first, "a second run must keep the fleet's existing authority"


def test_no_control_root_file_leaves_a_node_open(tmp_path):
    # The default has to stay what it was. Most deployments provision nothing here, and they
    # keep the in-band route: an open node authenticates nothing anyway, and an attacker in
    # radio range of one can already do worse than retune it.
    actuator = _CapturingActuator()
    edge, _ = _edge(tmp_path)
    edge.control_actuator = actuator

    edge.connector.inbox.put(_in_band_frame(edge, IN_BAND | RF_CONFIG, RF_BODY))
    edge.respond(edge._respond_handler)

    assert edge.control_root is None
    assert actuator.applied == [(RF_CONFIG, RF_BODY)], \
        "an unprovisioned node still takes in-band control"

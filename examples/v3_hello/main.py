# The deployed program, for every v3 node.
#
# It reads the config file beside it, builds what that file names, and runs it. This is the one
# file a board runs whatever it is: an Edge or a Hub, open or secure or control, on whichever
# radio. What distinguishes one deployment from another is the JSON, not a fork of this file.
#
# Copy this next to an AlLoRa.json (each folder beside it has one) and put both on the board as
# main.py and AlLoRa.json.
#
# See secure/edge/main_literal.py for the same deployment written out longhand, with every class
# named. That file is the one to read if you want to see what happens here without following a
# dispatch, and it is the starting point for a deployment this repo does not cover.
import gc

from AlLoRa.utils.file_utils import resolve_config_file
from AlLoRa.utils.json_utils import json


# The radio table lives here, in a file the deployment owns, and never inside AlLoRa/. Adding a
# radio this repo has never supported is one import and one branch: subclass Connector, supply
# its nine methods, and name it in the config. Were this table in the library, a new radio would
# need either a patched library or a plugin-registration API, and an unsupported radio would be
# a second-class citizen permanently.
#
# The imports are inside the branches for two reasons. A board carries one radio and has no
# flash to spare for three other drivers, and each of these modules imports MicroPython-only
# names, so a top-level import would stop this file being read anywhere but on a board.
def build_connector(driver):
    if driver == "sx127x":
        from AlLoRa.Connectors.SX127x_connector import SX127x_connector
        return SX127x_connector()
    if driver == "sx1262":
        from AlLoRa.Connectors.SX1262_connector import SX1262_connector
        return SX1262_connector()
    if driver == "e5":
        from AlLoRa.Connectors.E5_connector import E5_connector
        return E5_connector()
    if driver == "lopy4":
        from AlLoRa.Connectors.LoPy4_connector import LoPy4_connector
        return LoPy4_connector()
    # Never a fallback to a default radio. Coming up on the wrong chip is the exact failure the
    # driver key exists to close, and reaching it by way of a typo instead of a hardcoded import
    # would not make it any easier to see from the antenna.
    raise SystemExit(
        "connector.driver is '{}', which this program does not know. It builds: "
        "sx127x, sx1262, e5, lopy4. A radio that is not on that list is still supported: add it "
        "to build_connector above, and see secure/edge/main_literal.py.".format(driver))


def read_config(config_file=None):
    """The config file this board boots from, and what it says."""
    config_file = resolve_config_file(config_file)
    with open(config_file, "r") as f:
        return config_file, json.loads(f.read())


def wire_control(node):
    """Give an Edge whatever control it was provisioned for, and say which it got.

    Three postures, told apart by what the node came up holding rather than by a flag here. A
    node that holds a control root verifies signed artifacts against it and refuses unsigned
    commands from that moment, because a signature an unsigned frame could bypass would protect
    nothing.
    """
    from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
    actuator = Node_Control_Actuator(node)

    if node.control_root is None:
        # No root: commands are acted on because the link vouches for them, which in the secure
        # posture means the peer completed the handshake and in the open posture means nothing
        # at all. That is the honest trade for a survey link.
        node.control_actuator = actuator
        return "unsigned in-band commands are acted on"

    if node.device_id is None:
        # A rooted node on an open link. No verify gate can be built, and that is the design
        # rather than a gap: a signed artifact is addressed to a 32-byte device_id, and an open
        # node has no identity to be addressed by. Left running it would look provisioned and
        # never accept a command, so it stops here instead.
        raise SystemExit(
            "this node holds a control root but has no identity, so it could refuse unsigned "
            "commands and never verify a signed one. Set security_mode 'secure' plus "
            "identity_file in the config to finish provisioning.")

    from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink
    # The downlink sink IS the verify gate: only what it verifies against the provisioned root
    # reaches the actuator, and unsigned in-band commands are refused from here on.
    node.data_sink = Control_Root_DataSink(
        control_root=node.control_root,
        device_id=node.device_id,
        actuator=actuator,
        # Remembers the highest command number this node has accepted, so a command the Hub
        # already sent cannot be recorded off the air and replayed back at it later.
        counter_file=node.control_counter_file)
    return "verifying signed artifacts against the provisioned root"


def build_node(config, config_file, connector):
    """The node this config describes, wired and ready to run.

    Takes the connector rather than building one, so a deployment holding a radio this repo has
    never heard of reaches the same node through the same function.
    """
    # Which key is present is the declaration. A node config says which kind of node; a bridge
    # config carries `adapter` instead, and a bridge is not a third kind of node: it holds no
    # protocol logic, no keys and no session, so it is a different thing rather than a third
    # value of the same thing.
    node_type = config.get("node", None)
    if node_type is None:
        if "adapter" in config:
            raise SystemExit(
                "this config describes a bridge, not a node. Run the adapter program from "
                "examples/Adapters instead.")
        # A refusal, never a default. A config that declares nothing was written by something
        # that did not know this schema, and guessing would put a node on the air in a
        # placement nobody chose.
        raise SystemExit(
            "{} declares neither \"node\" nor \"adapter\", so there is nothing to run. Add "
            "\"node\": \"edge\" or \"node\": \"hub\".".format(config_file))

    if node_type == "edge":
        from AlLoRa.Nodes.Edge import Edge
        from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
        # What the Edge serves: the files waiting in its outbound folder, streamed from where
        # they lie rather than held in RAM. An empty folder is a node with nothing to send yet,
        # not an error and not a cue to invent something; it answers polls and waits.
        datasource = Disk_DataSource(queue_path=config.get("queue_path", "Outbox"))
        return Edge(connector, config_file=config_file, datasource=datasource)

    if node_type == "hub":
        from AlLoRa.Nodes.Hub import Hub
        return Hub(connector, config_file=config_file, nodes_file="Nodes.json")

    raise SystemExit(
        "\"node\" is '{}' in {}. A node is an \"edge\" or a \"hub\": named by where it sits, "
        "not by which way data flows.".format(node_type, config_file))


def main():
    gc.enable()

    config_file, config = read_config()
    print("config:", config_file)

    connector_config = config.get("connector", None)
    if not connector_config or connector_config.get("driver", None) is None:
        raise SystemExit(
            "{} names no connector.driver, so this program cannot tell which radio to build. A "
            "deployment that constructs its own Connector does not need the key; see "
            "secure/edge/main_literal.py for that shape.".format(config_file))

    node = build_node(config, config_file, build_connector(connector_config["driver"]))

    if config["node"] == "edge":
        print("EDGE ready | MAC:", node.MAC, "| mode:", node.security_mode,
              "| session:", node.session_id)
        if node.device_id is not None:
            # The one value that registers this node on its Hub. Stable across reboots for as
            # long as identity_file stays set and the file survives a reflash.
            print("EDGE device_id (register this on the Hub):", node.device_id.hex())
        print("EDGE serving from:", node.datasource.queue_path)
        print("control:", wire_control(node))
        node.run()
        return

    print("HUB ready | MAC:", node.MAC, "| mode:", node.security_mode,
          "| endpoints:", len(node.digital_endpoints))
    # An empty roster is the one failure that looks like a working Hub: it boots, it prints, and
    # it polls nobody. Registration is the whole of the setup on this side, so say so and stop.
    if not node.digital_endpoints:
        raise SystemExit(
            "Nodes.json registered no active endpoint, so this Hub would poll nobody. Add an "
            "entry, or set \"active\": true on one that is already there.")
    for endpoint in node.digital_endpoints:
        print("  polling", endpoint.get_name(), "as", endpoint.get_label(),
              "every", endpoint.asking_frequency, "s")

    # No actuator and no verify gate on this side. A Hub is the authority: it issues control
    # artifacts and is never commanded by one over the radio, so the half of a control root it
    # may hold is the signing half. Minting is driven by an operator calling ask_change_rf, not
    # by the loop below; control/hub/retune.py is that recipe.
    if node.control_root is not None:
        print("control: this Hub holds a control root and can sign commands")

    node.run(save_files=True)


# A board runs main.py as __main__, so this guard costs a deployment nothing and lets the file
# be imported and checked by tests/test_generic_main.py, which is what keeps the dispatch above
# honest about what it builds.
if __name__ == "__main__":
    main()

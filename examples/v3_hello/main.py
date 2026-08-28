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


# The board table, beside the radio table and for the same reason: a board is a pin map and a
# list of what is soldered to it, so it belongs to the deployment rather than to the protocol.
# Adding a board this repo has never seen is one import and one branch, in a file you own.
def build_board(name):
    if name == "t3s3":
        from lora32 import T3S3
        return T3S3()
    raise SystemExit(
        "device.board is '{}', which this program does not know. It builds: t3s3. A board that "
        "is not on that list needs one import and one branch in build_board above.".format(name))


# What the screen draws. Both layouts reserve the left 40 pixels for the logo and start text at
# x=40, which is what makes them fit a 128x32 display.
_EDGE_LAYOUT = [
    {'key': 'MAC',   'pos': {'x': 40, 'y': 0},  'area': {'x': 40, 'y': 0,  'w': 88, 'h': 12}, 'static': True},
    {'key': 'BW',    'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 30, 'h': 12}},
    {'key': 'TX_P',  'pos': {'x': 70, 'y': 12}, 'area': {'x': 70, 'y': 12, 'w': 25, 'h': 12}},
    {'key': 'SNR',   'pos': {'x': 95, 'y': 12}, 'area': {'x': 95, 'y': 12, 'w': 30, 'h': 12}},
    {'key': 'RSSI',  'pos': {'x': 40, 'y': 24}, 'area': {'x': 40, 'y': 24, 'w': 40, 'h': 12}},
    {'key': 'Chunk', 'pos': {'x': 80, 'y': 24}, 'area': {'x': 80, 'y': 24, 'w': 30, 'h': 12}},
    {'key': 'SF',    'pos': {'x': 110, 'y': 24}, 'area': {'x': 110, 'y': 24, 'w': 30, 'h': 12}},
]
# The Hub layout used to ask for a key called 'Signal'. Nothing has ever published it, so that
# row rendered blank; RSSI is the value it was reaching for.
_HUB_LAYOUT = [
    {'key': 'MAC',   'pos': {'x': 40, 'y': 0},  'area': {'x': 40, 'y': 0,  'w': 88, 'h': 12}, 'static': True},
    {'key': 'File',  'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 88, 'h': 12}},
    {'key': 'RSSI',  'pos': {'x': 40, 'y': 24}, 'area': {'x': 40, 'y': 24, 'w': 40, 'h': 12}},
    {'key': 'Chunk', 'pos': {'x': 80, 'y': 24}, 'area': {'x': 80, 'y': 24, 'w': 40, 'h': 12}},
]


def wants(device, key, board_has):
    """Whether this deployment wants a peripheral on, and whether the board can give it.

    Say nothing and you get what the board has, so a board with a screen shows the link without
    anybody configuring it. Ask for something the board does not have and this stops: a config
    naming a screen on a board with no screen was written against a different board, and running
    on anyway would hide that until somebody stood next to the antenna wondering why it is dark.
    """
    asked = device.get(key, None)
    if asked is None:
        return board_has
    if asked and not board_has:
        raise SystemExit(
            "device.{} is true, but {} says this board has no {}. Either the config was written "
            "for a different board, or the board file needs to declare it.".format(
                key, device.get("board", "the board file"), key))
    return bool(asked)


def prepare_device(config):
    """Build the board and mount its card, before anything asks the filesystem for a queue.

    Returns the board, or None when this config names no device section. Nothing else here
    touches the node, because the node does not exist yet: a Disk_DataSource pointed at a card
    has to find the card already mounted.
    """
    device = config.get("device", None)
    if not device:
        return None, []

    board = build_board(device.get("board", None))
    notes = []

    if wants(device, "sd", board.HAS_SD):
        queue_path = config.get("queue_path", "") or config.get("result_path", "")
        mount_point = device.get("sd_mount_point", None)
        try:
            notes.append("sd mounted at " + board.mount_sd(path=mount_point))
        except Exception as e:
            # Fatal only when the files this node exists to move live on the card. A card that
            # was only going to hold logs is not worth refusing to run over.
            root = mount_point or board.SD_MOUNT_POINT
            if queue_path.startswith(root):
                raise SystemExit(
                    "the card would not mount ({}), and this node reads or writes {} on it, so "
                    "it would run and move nothing. Check the card is seated and "
                    "formatted.".format(e, queue_path))
            notes.append("no card ({}), and nothing this node moves lives on it".format(e))

    return board, notes


def wire_device(config, node, board):
    """Register the peripherals this deployment asked for, and say what came up.

    Everything here is deployment wiring, not protocol: a screen and a log are objects with an
    `update` method, registered through the subscriber seam the node already has. The library
    never learns what either of them is.
    """
    device = config.get("device", None)
    if not device or board is None:
        return []

    switched_on = []

    if wants(device, "screen", board.HAS_SCREEN):
        from board.oled_screen import OLED_Screen
        layout = _HUB_LAYOUT if config.get("node", None) == "hub" else _EDGE_LAYOUT
        # The logo is a file on the board, not part of the program. A deployment that did not
        # copy it gets the readings and no logo, rather than a node that will not boot.
        img_data = None
        logo = device.get("logo_file", "AlLoRa_logo.json")
        try:
            with open(logo, "r") as f:
                img_data = json.loads(f.read())
        except Exception:
            switched_on.append("no logo at " + logo + ", readings only")
        screen = OLED_Screen(board, img_data, layout_config=layout,
                             button=device.get("screen_button", True))
        node.register_subscriber(screen)
        switched_on.append("screen")

    if wants(device, "led", board.HAS_LED):
        from board.led_alive import LED
        LED(board).run()
        switched_on.append("led")

    log_file = device.get("log_file", None)
    if log_file:
        from AlLoRa.Subscribers.Logger import Logger
        node.register_subscriber(Logger(
            log_file=log_file,
            always_log_topics=["RSSI", "SNR", "Chunk", "PSizeS", "PSizeR",
                               "TimePR", "TimePS", "TimeBtw"],
            change_log_topics=["File", "Status", "Retransmission", "CorruptedPackets",
                               "Freq", "SF", "BW", "CR", "TX_P"]))
        switched_on.append("log to " + log_file)

    if switched_on:
        node.notify_subscribers()
    return switched_on


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

    # Before the node, because a queue that lives on a card needs the card mounted first.
    board, notes = prepare_device(config)
    for note in notes:
        print("device:", note)

    node = build_node(config, config_file, build_connector(connector_config["driver"]))

    for note in wire_device(config, node, board):
        print("device:", note)

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

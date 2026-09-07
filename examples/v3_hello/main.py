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
import time

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


# The boundary table, beside the radio table and the board table, and never inside AlLoRa/. A
# library that grew a table of legal boundaries would start deciding which compositions are
# blessed, which is the one thing v3 promises not to do, and it would drag paho into the import
# path of every deployment that publishes nothing.
#
# Each entry lists the keys that kind accepts. A key outside its list stops the boot rather than
# being ignored, so "hosts" is a halt and not a silent fall back to localhost: a boundary that
# publishes into the wrong place looks exactly like one that works.
_SINK_KINDS = {
    "disk": (),
    "mqtt": ("host", "port", "topic_prefix", "client_id", "qos", "retain", "keepalive",
             "cleanup"),
    "http": ("url", "token", "timeout", "cleanup"),
}
_SOURCE_KINDS = {
    "disk": ("queue_path", "file_queue_size"),
    "mqtt": ("host", "port", "topics", "client_id", "keepalive", "file_queue_size"),
}


def _boundary_args(block, table, key_name, reserved=()):
    """Read one boundary block: which kind it names, and what to hand the constructor.

    Only the keys present are passed on, so every default stays where the class defines it and
    there is one place to read it. `reserved` names keys this config already spells somewhere
    else, which are refused here rather than quietly shadowing the outer one.
    """
    kind = block.get("kind", None)
    if kind is None:
        raise SystemExit(
            "the {} block names no kind. It is one of: {}.".format(
                key_name, ", ".join(sorted(table))))
    if kind not in table:
        # Never a fall back to disk. A config naming a boundary this program cannot build was
        # written against a different program, and running it as disk gives a node that looks
        # provisioned, delivers nowhere and says nothing.
        raise SystemExit(
            "{}.kind is '{}', which this program does not know. It builds: {}. A boundary that "
            "is not on that list is still supported: pass it to the constructor, as "
            "examples/Hubs/Many-Edges/USB/main_mqtt.py does.".format(
                key_name, kind, ", ".join(sorted(table))))
    args = {}
    for key in block:
        if key == "kind":
            continue
        if key in reserved:
            raise SystemExit(
                "{} names '{}' inside the block, but this config already spells it at the top "
                "level. Keep the one that is there and remove this one, so there is a single "
                "place to read it.".format(key_name, key))
        if key not in table[kind]:
            raise SystemExit(
                "{} names '{}', which a '{}' boundary does not take. It takes: {}.".format(
                    key_name, key, kind, ", ".join(table[kind]) or "nothing but kind"))
        args[key] = block[key]
    return kind, args


def build_data_sink(block, key_name="data_sink"):
    """The sink this block names, or None for the disk sink the node builds itself.

    Disk returns None rather than a Disk_DataSink deliberately: the node builds one lazily from
    result_path when a file first completes, so handing it one at construction would be a nearly
    identical node instead of an identical one, and the two would drift the day that lazy path
    changes.
    """
    kind, args = _boundary_args(block, _SINK_KINDS, key_name, reserved=("result_path",))
    if kind == "disk":
        return None
    if kind == "http":
        # The first boundary with a key that has no defensible default. Every mqtt key can fall
        # back to the class, because a Hub beside its broker is the ordinary deployment and
        # localhost is usually right; there is no equivalent guess for which website a
        # deployment's data belongs to. Halted here rather than left to the constructor so the
        # operator gets the same one-line boot refusal every other config mistake gives them,
        # instead of a traceback out of a library.
        if "url" not in args:
            raise SystemExit(
                "{}.kind is 'http' but the block names no url. It takes: {}.".format(
                    key_name, ", ".join(_SINK_KINDS["http"])))
        from AlLoRa.DataSinks.HTTP_DataSink import HTTP_DataSink
        return HTTP_DataSink(**args)
    from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink
    return MQTT_DataSink(**args)


def build_datasource(block, key_name="datasource", queue_path=None, reserved=()):
    """The source this block names.

    `queue_path` is what a disk source falls back to when the block does not carry one, which is
    how the node-level key stays the single home for an outbox folder. A roster entry has no
    outer key to defer to, so there it carries its own.
    """
    kind, args = _boundary_args(block, _SOURCE_KINDS, key_name, reserved=reserved)
    if kind == "disk":
        from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
        if queue_path is not None and "queue_path" not in args:
            args["queue_path"] = queue_path
        return Disk_DataSource(**args)
    from AlLoRa.DataSources.MQTT_DataSource import MQTT_DataSource
    if "topics" in args:
        # JSON gives a list; the source keeps a tuple.
        args["topics"] = tuple(args["topics"])
    return MQTT_DataSource(**args)


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


def wire_downlink_sources(node):
    """Give each endpoint whatever downlink source its roster entry names, and say which got one.

    A Hub serves a different downlink to each Edge, so the source is a property of the endpoint
    and its block sits in that endpoint's entry rather than in the Hub's own config.

    Read here rather than inside add_digital_endpoints on purpose. The Hub would have to hold a
    kind-to-class table to do it, and that table belongs to the deployment: a Hub that knew which
    boundaries are legal would start deciding which compositions are blessed. So the library
    keeps a public verb, set_downlink_source, and this file supplies the object.

    Registration happens here, before the loop starts, because set_downlink_source calls
    prepare(): a broker that cannot be reached should fail while somebody is watching, not
    several visits into a drive loop where it reads as an Edge that never receives anything.
    """
    from AlLoRa.Digital_Endpoint import label_for_config
    if not node.nodes_file:
        return []
    try:
        with open(node.nodes_file, "r") as f:
            roster = json.loads(f.read())
    except (OSError, ValueError):
        # The Hub already reported a roster it could not read, and it is running on whatever it
        # registered. Saying it twice helps nobody.
        return []

    blocks = {}
    for entry in roster:
        block = entry.get("datasource", None)
        if block:
            blocks[label_for_config(entry)] = block

    wired = []
    for endpoint in node.digital_endpoints:
        block = blocks.pop(endpoint.get_label(), None)
        if not block:
            continue
        node.set_downlink_source(
            endpoint, build_datasource(block, key_name="datasource for " + endpoint.get_name()))
        wired.append("{} serves downlink from {}".format(endpoint.get_name(), block["kind"]))

    # An entry that names a source and never becomes an endpoint is a block nobody will read:
    # the entry is inactive, or its label was edited on one side only. Silence here is how a
    # deployment ends up wondering why one Edge receives nothing.
    for label in blocks:
        wired.append(
            "WARNING: an entry labelled {} names a datasource but is not a registered endpoint, "
            "so nothing serves it. Check \"active\" and its address.".format(label))
    return wired


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
        # What the Edge serves: by default the files waiting in its outbound folder, streamed
        # from where they lie rather than held in RAM. An empty folder is a node with nothing to
        # send yet, not an error and not a cue to invent something; it answers polls and waits.
        datasource = build_datasource(config.get("datasource", None) or {"kind": "disk"},
                                      queue_path=config.get("queue_path", "Outbox"),
                                      reserved=("queue_path",))

        sink_block = config.get("data_sink", None)
        if sink_block and config.get("control_root_file", None):
            # A node that answers to an authority keeps its verify gate in the sink slot, and
            # that gate is the only thing standing between a signed command and an unsigned one.
            # A config key able to displace it would let a text edit disarm the signature check,
            # so a config asking for both is refused where a human can still see it rather than
            # resolved in favour of either.
            raise SystemExit(
                "this config names both control_root_file and data_sink. A node holding a "
                "control root puts its verify gate in the sink slot, so the two cannot both "
                "have it. Remove the data_sink block, or remove control_root_file if this node "
                "is not meant to answer to a control root.")

        return Edge(connector, config_file=config_file, datasource=datasource,
                    data_sink=build_data_sink(sink_block) if sink_block else None)

    if node_type == "hub":
        from AlLoRa.Nodes.Hub import Hub
        if config.get("datasource", None):
            # Not a refusal of the idea, a redirection to the right file. A Hub serves a
            # different downlink to each Edge it holds, so one source at the top of its own
            # config would say every Edge is served the same file, which is the one thing a
            # per-endpoint downlink exists not to say.
            raise SystemExit(
                "a Hub serves a different downlink to each Edge, so a datasource block belongs "
                "in that Edge's entry in Nodes.json, not at the top of this file.")
        sink_block = config.get("data_sink", None)
        return Hub(connector, config_file=config_file, nodes_file="Nodes.json",
                   data_sink=build_data_sink(sink_block) if sink_block else None)

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
        # An MQTT source has no folder to name, so say what it is rather than reach for one.
        print("EDGE serving from:", getattr(node.datasource, "queue_path", node.datasource))
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

    for note in wire_downlink_sources(node):
        print("downlink:", note)

    # No actuator and no verify gate on this side. A Hub is the authority: it issues control
    # artifacts and is never commanded by one over the radio, so the half of a control root it
    # may hold is the signing half. Minting is driven by an operator calling ask_change_rf, not
    # by the loop below; control/hub/retune.py is that recipe.
    if node.control_root is not None:
        print("control: this Hub holds a control root and can sign commands")

    node.run(save_files=True)


def halt(reason, interval=10):
    """Stop where somebody can read why, which on this port is not what stopping normally does.

    Every refusal above raises SystemExit carrying a sentence written for whoever has to fix the
    config. CPython would print it. MicroPython prints nothing for SystemExit, and turns an
    uncaught one out of main.py into a forced exit, which the ESP32 port makes a soft reset: the
    board reboots, re-reads the same config, refuses again, and repeats. A board cycling with one
    line of output looks like a hardware fault rather than a config with a typo in it.

    So say the reason, and keep saying it, without ever handing control back to the runtime.
    Somebody attaching a cable ten minutes from now still learns why. Only SystemExit is caught,
    so Ctrl-C still reaches the REPL from here.
    """
    while True:
        print("STOPPED:", reason)
        time.sleep(interval)


def run():
    """What a board runs: the program, and a refusal it can be read off the serial port."""
    try:
        main()
    except SystemExit as stop:
        halt(stop.args[0] if stop.args else "stopped, and gave no reason")


# A board runs main.py as __main__, so this guard costs a deployment nothing and lets the file
# be imported and checked by tests/test_generic_main.py, which is what keeps the dispatch above
# honest about what it builds.
if __name__ == "__main__":
    run()

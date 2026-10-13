# The program every v3 node runs. It builds what the AlLoRa.json beside it names, and runs it.
# Edge or Hub, open, secure or control, any radio: the JSON is what differs, not this file.
# Put it on the board as main.py, next to the AlLoRa.json from one of the folders here.
# secure/edge/main_literal.py is the same deployment written out with every class named.
import gc
import time

from AlLoRa.utils.file_utils import resolve_config_file
from AlLoRa.utils.json_utils import json


# The radios this program can build, by connector.driver. A new radio is one import and one branch
# here, not a library change. Each import sits in its branch: a board carries one radio's driver.
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
    # No default radio: a typo must not bring the board up on the wrong chip.
    raise SystemExit(
        "connector.driver is '{}', which this program does not know. It builds: "
        "sx127x, sx1262, e5, lopy4. A radio that is not on that list is still supported: add it "
        "to build_connector above, and see secure/edge/main_literal.py.".format(driver))


# The boards this program knows, by device.board. A new board is one import and one branch here.
def build_board(name):
    if name == "t3s3":
        from lora32 import T3S3
        return T3S3()
    raise SystemExit(
        "device.board is '{}', which this program does not know. It builds: t3s3. A board that "
        "is not on that list needs one import and one branch in build_board above.".format(name))


# The DataSink and DataSource kinds this program builds, and the keys each one takes. Any other
# key stops the boot, so a typo such as "hosts" never falls back to localhost without a word.
_SINK_KINDS = {
    "disk": (),
    "mqtt": ("host", "port", "topic_prefix", "client_id", "qos", "retain", "keepalive",
             "cleanup"),
    "http": ("url", "token", "timeout", "cleanup"),
}
_SOURCE_KINDS = {
    "disk": ("queue_path", "file_queue_size", "cleanup", "archive_path", "archive_budget"),
    "mqtt": ("host", "port", "topics", "client_id", "keepalive", "file_queue_size"),
    "serial": ("queue_path", "file_queue_size", "cleanup", "archive_path", "archive_budget",
               "uart_id", "baudrate", "tx", "rx", "link_chunk_size", "name_length",
               "shorten_names", "stall_timeout"),
}


def _boundary_args(block, table, key_name, reserved=()):
    """Read one block: its kind, and the keys to pass to the constructor (defaults stay in the
    class). Keys in `reserved` are set at the top level of the config, so they are refused here."""
    kind = block.get("kind", None)
    if kind is None:
        raise SystemExit(
            "the {} block names no kind. It is one of: {}.".format(
                key_name, ", ".join(sorted(table))))
    if kind not in table:
        # No fallback to disk: the node would look fine and deliver nowhere.
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
    """The sink this block names, or None for disk: the node builds its own disk sink."""
    kind, args = _boundary_args(block, _SINK_KINDS, key_name, reserved=("result_path",))
    if kind == "disk":
        return None
    if kind == "http":
        # A url has no sensible default. Refused here so the message is one line, not a traceback.
        if "url" not in args:
            raise SystemExit(
                "{}.kind is 'http' but the block names no url. It takes: {}.".format(
                    key_name, ", ".join(_SINK_KINDS["http"])))
        from AlLoRa.DataSinks.HTTP_DataSink import HTTP_DataSink
        return HTTP_DataSink(**args)
    from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink
    return MQTT_DataSink(**args)


def build_datasource(block, key_name="datasource", queue_path=None, reserved=()):
    """The source this block names. A disk source without its own queue_path uses `queue_path`."""
    kind, args = _boundary_args(block, _SOURCE_KINDS, key_name, reserved=reserved)
    if kind in ("disk", "serial"):
        # A serial source is a disk outbox filled over a cable, so both use the same queue_path.
        if queue_path is not None and "queue_path" not in args:
            args["queue_path"] = queue_path
        if kind == "serial":
            from AlLoRa.DataSources.Serial_DataSource import Serial_DataSource
            return Serial_DataSource(**args)
        from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
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
_HUB_LAYOUT = [
    {'key': 'MAC',   'pos': {'x': 40, 'y': 0},  'area': {'x': 40, 'y': 0,  'w': 88, 'h': 12}, 'static': True},
    {'key': 'File',  'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 88, 'h': 12}},
    {'key': 'RSSI',  'pos': {'x': 40, 'y': 24}, 'area': {'x': 40, 'y': 24, 'w': 40, 'h': 12}},
    {'key': 'Chunk', 'pos': {'x': 80, 'y': 24}, 'area': {'x': 80, 'y': 24, 'w': 40, 'h': 12}},
]


def wants(device, key, board_has):
    """Whether to switch a peripheral on. Left out of the config, it is on if the board has it.
    Asked for on a board without it, the boot stops: the config was written for another board."""
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
    """Build the board and mount its card, before the node opens a queue on it.
    Returns (board, notes), or (None, []) when the config has no device section."""
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
    """Register the screen, LED and log the config asks for, as node subscribers, and list what
    came up."""
    device = config.get("device", None)
    if not device or board is None:
        return []

    switched_on = []

    if wants(device, "screen", board.HAS_SCREEN):
        from board.oled_screen import OLED_Screen
        layout = _HUB_LAYOUT if config.get("node", None) == "hub" else _EDGE_LAYOUT
        # Without the logo file the screen shows the readings alone.
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
    """Give an Edge the control it was provisioned for, and say which. A node holding a control
    root accepts only signed commands."""
    from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
    actuator = Node_Control_Actuator(node)

    if node.control_root is None:
        # No root: commands are trusted as far as the link is, the handshake in secure and
        # nothing in open.
        node.control_actuator = actuator
        return "unsigned in-band commands are acted on"

    if node.device_id is None:
        # A signed command is addressed to a device_id, and an open node has none.
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
    """Give each Edge the downlink source its Nodes.json entry names, and list which got one.
    Done before the loop starts, so an unreachable broker fails at boot, not several visits in."""
    from AlLoRa.Digital_Endpoint import label_for_config
    if not node.nodes_file:
        return []
    try:
        with open(node.nodes_file, "r") as f:
            roster = json.loads(f.read())
    except (OSError, ValueError):
        # The Hub has already reported a Nodes.json it could not read.
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

    # An entry with a datasource that is not an endpoint: inactive, or its address was changed.
    for label in blocks:
        wired.append(
            "WARNING: an entry labelled {} names a datasource but is not a registered endpoint, "
            "so nothing serves it. Check \"active\" and its address.".format(label))
    return wired


def build_node(config, config_file, connector):
    """The node this config describes. Takes the connector, so an unlisted radio works too."""
    # "node" says edge or hub. A bridge config has "adapter" instead and runs another program.
    node_type = config.get("node", None)
    if node_type is None:
        if "adapter" in config:
            raise SystemExit(
                "this config describes a bridge, not a node. Run the adapter program from "
                "examples/Adapters instead.")
        # No default: a guess would put a node on the air in a role nobody chose.
        raise SystemExit(
            "{} declares neither \"node\" nor \"adapter\", so there is nothing to run. Add "
            "\"node\": \"edge\" or \"node\": \"hub\".".format(config_file))

    if node_type == "edge":
        from AlLoRa.Nodes.Edge import Edge
        # By default the Edge serves the files in its outbox folder; empty means nothing to send.
        datasource = build_datasource(config.get("datasource", None) or {"kind": "disk"},
                                      queue_path=config.get("queue_path", "Outbox"),
                                      reserved=("queue_path",))

        sink_block = config.get("data_sink", None)
        if sink_block and config.get("control_root_file", None):
            # A node with a control root checks signatures in the sink slot; a data_sink there
            # would switch that check off.
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
            # A Hub's downlink differs per Edge, so the block goes in that Edge's Nodes.json entry.
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
            # Register this on the Hub. It stays the same across reboots while identity_file is set.
            print("EDGE device_id (register this on the Hub):", node.device_id.hex())
        # An MQTT source has no folder to name, so say what it is rather than reach for one.
        print("EDGE serving from:", getattr(node.datasource, "queue_path", node.datasource))
        print("control:", wire_control(node))
        node.run()
        return

    print("HUB ready | MAC:", node.MAC, "| mode:", node.security_mode,
          "| endpoints:", len(node.digital_endpoints))
    # A Hub with no Edges would boot and poll nobody, which looks like a working Hub.
    if not node.digital_endpoints:
        raise SystemExit(
            "Nodes.json registered no active endpoint, so this Hub would poll nobody. Add an "
            "entry, or set \"active\": true on one that is already there.")
    for endpoint in node.digital_endpoints:
        print("  polling", endpoint.get_name(), "as", endpoint.get_label(),
              "for", endpoint.listening_time, "s, then resting",
              endpoint.wait_after_visit, "s")

    for note in wire_downlink_sources(node):
        print("downlink:", note)

    # A Hub signs commands and never receives them. control/hub/retune.py shows how to send one.
    if node.control_root is not None:
        print("control: this Hub holds a control root and can sign commands")

    node.run(save_files=True)


def halt(reason, interval=10):
    """Print the reason every few seconds, and never return.

    On the ESP32, MicroPython prints nothing for a SystemExit and soft-resets, so the board would
    reboot in a loop without saying why. Ctrl-C still reaches the REPL."""
    while True:
        print("STOPPED:", reason)
        time.sleep(interval)


def run():
    """What a board runs: the program, and a refusal it can be read off the serial port."""
    try:
        main()
    except SystemExit as stop:
        halt(stop.args[0] if stop.args else "stopped, and gave no reason")


# The guard lets tests/test_generic_main.py import this file.
if __name__ == "__main__":
    run()

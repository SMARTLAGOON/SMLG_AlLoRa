# The same deployment as ../../main.py, written out in full for the secure Edge.
# Read it to see what main.py builds. Copy it when the config cannot describe your deployment,
# for example a radio this library does not support: subclass Connector, write its nine
# methods, and pass an instance below.
# tests/test_generic_main.py checks that this file and main.py build the same node.
import gc
import time

from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Data.Sources.Disk_DataSource import Disk_DataSource
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator

gc.enable()


def halt(reason, interval=10):
    """Print the reason every few seconds, and never return. A SystemExit would soft-reset the
    board in a silent loop, as ../../main.py explains. Ctrl-C still reaches the REPL."""
    while True:
        print("STOPPED:", reason)
        time.sleep(interval)


# The radio, named here instead of in the config. A Connector handed to the node wins, and the
# `driver` key is not consulted at all.
connector = SX127x_connector()

# The Edge serves the files in this folder; empty means nothing to send yet.
data_source = Disk_DataSource(queue_path="Outbox")

edge = Edge(connector, config_file="AlLoRa.json", data_source=data_source)
print("EDGE ready | MAC:", edge.MAC, "| mode:", edge.security_mode, "| session:", edge.session_id)

# This file is the secure setup: an identity, and no control root.
if edge.device_id is None:
    halt("This node has no identity. Set security_mode 'secure' plus identity_file "
         "in AlLoRa.json, or load the ../../open config instead.")
if edge.control_root is not None:
    halt("This node holds a control root. Load the ../../control config instead.")

# Register this on the Hub. It stays the same across reboots while identity_file is set.
print("EDGE device_id (register this on the Hub):", edge.device_id.hex())
print("EDGE serving from:", data_source.queue_path)

# Acts on commands from the Hub that completed the handshake, after acknowledging them. For
# signed commands, see ../../control.
edge.control_actuator = Node_Control_Actuator(edge)
print("control: unsigned in-band commands are acted on")

edge.run()

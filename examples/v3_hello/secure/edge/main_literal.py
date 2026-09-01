# The same deployment as ../../main.py, written out longhand.
#
# ../../main.py reads the config and builds what it names. This file names everything itself, for
# the secure Edge, and does nothing else different: same classes, same order, same node. Read it
# to see what the dispatch actually does, and copy it when your deployment is not one the config
# can describe.
#
# The case that matters is a radio this repository has never supported. `Node` takes a Connector
# INSTANCE and stores it, so nothing here needs a `driver` name or a patched library: subclass
# `Connector`, supply its nine methods (`config`, `transmit`, `recv`, `get_rssi`, `get_snr`, and
# the five RF setters), and pass it on the line below. `SX127x_connector` is 132 lines and
# `E5_connector` fits the same shape while being an AT-command modem rather than an SPI
# transceiver, so the seam is wide enough for most radios.
#
# `tests/test_generic_main.py` runs this file and the generic one over a loopback and requires
# the same node out of both, because a literal file that has drifted is worse than none: it is
# the one people read.
import gc
import time

from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator

gc.enable()


def halt(reason, interval=10):
    """Stop where somebody can read why. Written out here like everything else in this file.

    MicroPython prints nothing for SystemExit and turns an uncaught one into a soft reset, so a
    stop written as a raise would reboot the board into the same refusal, silently, forever. Say
    it instead, and keep saying it, without handing control back to the runtime. Ctrl-C still
    reaches the REPL from here. ../../main.py carries the same function for the same reason.
    """
    while True:
        print("STOPPED:", reason)
        time.sleep(interval)


# The radio, named here instead of in the config. A Connector handed to the node wins, and the
# `driver` key is not consulted at all.
connector = SX127x_connector()

# What the Edge serves: files waiting in this folder, streamed from where they lie rather than
# held in RAM. An empty folder is a node with nothing to send yet; it answers polls and waits.
datasource = Disk_DataSource(queue_path="Outbox")

edge = Edge(connector, config_file="AlLoRa.json", datasource=datasource)
print("EDGE ready | MAC:", edge.MAC, "| mode:", edge.security_mode, "| session:", edge.session_id)

# This file is the secure posture: an identity, and no control root. The generic program handles
# all three by looking at what the node came up holding; here they are simply stated.
if edge.device_id is None:
    halt("This node has no identity. Set security_mode 'secure' plus identity_file "
         "in AlLoRa.json, or load the ../../open config instead.")
if edge.control_root is not None:
    halt("This node holds a control root. Load the ../../control config instead.")

# The one value that registers this node on its Hub. Its first 4 bytes address first contact, so
# no session_id is hand-assigned the way the open pair needs. Stable across reboots for as long
# as identity_file stays set and the file survives a reflash.
print("EDGE device_id (register this on the Hub):", edge.device_id.hex())
print("EDGE serving from:", datasource.queue_path)

# Queues the effect of a command rather than performing it, so the node applies it only once the
# acknowledgement is on the air: switching the radio first would cost the Hub its confirmation.
# Sealed frames authenticate the link itself, so a command that arrives here came from the node
# that completed the handshake. It is not a signed artifact, and it does not survive being
# relayed. That is ../../control.
edge.control_actuator = Node_Control_Actuator(edge)
print("control: unsigned in-band commands are acted on")

edge.run()

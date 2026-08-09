# v3 hello-world, OPEN mode: the Edge (the node that HAS the data and serves it).
# Flash the AlLoRa firmware, then put this + LoRa.json on the device as main.py + LoRa.json.
#
# Open is the survey posture: no identity, no keys, nothing authenticated in either direction.
# Get a file across here first, then move to ../../secure.
import gc
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator

gc.enable()

edge = Edge(SX127x_connector(), config_file="LoRa.json")
print("EDGE ready | MAC:", edge.MAC, "| session:", edge.session_id)

# This example is the open posture and nothing else. A config that says otherwise is the wrong
# file on the board, not a case to handle here. One main.py that branched on whatever it found
# is what let an untested combination sit unnoticed until it raised on a bench, so each posture
# now states what it needs and stops if it is not that.
if edge.control_root is not None:
    raise SystemExit("This node holds a control root. Load the ../../control example instead.")

# What acts on a control command the Hub sends (an RF-config change, a reset). It queues the
# effect rather than performing it, so the node applies it only once the acknowledgement is on
# the air: switching the radio first would cost the Hub its confirmation. Wired after
# construction because it acts on the node it is handed.
#
# Nothing authenticates these commands. That is the honest trade for an open survey link, which
# authenticates nothing in either direction. Leave this line out and the node quietly ignores
# every command instead; ../../control is where a command becomes something a node can check.
edge.control_actuator = Node_Control_Actuator(edge)
print("EDGE has no control root: unsigned in-band commands are acted on")

# a 1000-byte test file (~5 chunks at chunk_size 200)
payload = bytes((i % 256) for i in range(1000))

while True:
    if not edge.got_file():
        edge.set_file(AlLoRa_File(name="hello.bin",
                                  content=bytearray(payload),
                                  chunk_size=edge.get_chunk_size()))
        print("file set:", edge.file.get_name())
    # serves the file to whoever asks (handles OK -> metadata -> chunks -> final OK); returns
    # once fully sent, then re-arms.
    edge.send_file()
    print("file delivered")

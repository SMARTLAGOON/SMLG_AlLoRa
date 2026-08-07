# v3 hello-world: the Edge (the node that HAS the data and serves it).
# Flash the AlLoRa firmware, then put this + LoRa.json on the device as main.py + LoRa.json.
import gc
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink

gc.enable()

edge = Edge(SX127x_connector(), config_file="LoRa.json")
print("EDGE ready | MAC:", edge.MAC, "| session:", edge.session_id)
# In secure mode the Edge has a crypto identity: register THIS device_id on the Hub
# (its first 4 bytes address first contact; its first byte seeds the session id). It is stable
# across reboots as long as identity_file is set in LoRa.json.
if edge.device_id is not None:
    print("EDGE device_id (register this on the Hub):", edge.device_id.hex())

# What acts on a control command the Hub sends (an RF-config change, a reset). It queues the
# effect rather than performing it, so the node applies it only once the acknowledgement is on
# the air: switching the radio first would cost the Hub its confirmation. Wired after
# construction because it acts on the node it is handed.
actuator = Node_Control_Actuator(edge)
if edge.control_root is None:
    # Nothing provisioned, so commands arrive on the link itself with nothing authenticating
    # them. That is the honest trade for an open survey link, which authenticates nothing in
    # either direction; leave this line out and the node quietly ignores every command instead.
    edge.control_actuator = actuator
else:
    # A control root is provisioned, so the downlink sink IS the verify gate and only what it
    # verifies reaches the actuator. Unsigned in-band commands are refused from here on: if one
    # still worked, the signature would be protecting nothing.
    edge.data_sink = Control_Root_DataSink(
        control_root=edge.control_root,
        device_id=edge.device_id,
        actuator=actuator,
        # Remembers the highest command number this node has accepted, so a command the Hub
        # already sent cannot be recorded off the air and replayed back at it later.
        counter_file=edge.config.get("control_counter_file", "control.counter"))
    print("EDGE verifying signed control against the provisioned root")

# a 1000-byte test file (~5 chunks at chunk_size 200)
payload = bytes((i % 256) for i in range(1000))

while True:
    if not edge.got_file():
        edge.set_file(AlLoRa_File(name="hello.bin",
                                  content=bytearray(payload),
                                  chunk_size=edge.get_chunk_size()))
        print("file set:", edge.file.get_name())
    # serves the file to whoever asks (handles OK -> metadata -> chunks -> final OK, incl. the
    # secure handshake if security_mode is "secure"); returns once fully sent, then re-arms.
    edge.send_file()
    print("file delivered")

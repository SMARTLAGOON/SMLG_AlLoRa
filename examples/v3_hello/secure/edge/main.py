# v3 hello-world, SECURE mode: the Edge (the node that HAS the data and serves it).
# Flash the AlLoRa firmware, then put this + LoRa.json on the device as main.py + LoRa.json.
#
# Same transfer as ../../open, with a crypto identity underneath it: the ECDH handshake runs
# automatically on first contact and every frame after it is AEAD-sealed. Needs the CTR-flag
# firmware, which the CI build has.
import gc
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator

gc.enable()

edge = Edge(SX127x_connector(), config_file="LoRa.json")
print("EDGE ready | MAC:", edge.MAC, "| session:", edge.session_id)

# This example is the secure posture and nothing else; see the note in ../../open/edge/main.py
# for why each posture states what it needs rather than branching on what it finds.
if edge.device_id is None:
    raise SystemExit("This node has no identity. Set security_mode 'secure' plus identity_file "
                     "in LoRa.json, or load the ../../open example instead.")
if edge.control_root is not None:
    raise SystemExit("This node holds a control root. Load the ../../control example instead.")

# Register THIS device_id on the Hub. Its first 4 bytes address first contact (no MAC on the
# wire) and its first byte seeds the session id, which is why the secure pair needs no
# hand-assigned session_id the way the open one does. It is stable across reboots for as long
# as identity_file stays set in LoRa.json and the file survives a reflash.
print("EDGE device_id (register this on the Hub):", edge.device_id.hex())

# Queues the effect of a control command rather than performing it, so the node applies it only
# once the acknowledgement is on the air: switching the radio first would cost the Hub its
# confirmation. Sealed frames authenticate the link itself, so a command that arrives here came
# from the node that completed the handshake. It is not a signed artifact: it is not checked
# against a fleet authority and it does not survive being relayed. That is ../../control.
edge.control_actuator = Node_Control_Actuator(edge)
print("EDGE has no control root: commands are trusted because the link is sealed")

# a 1000-byte test file (~5 chunks at chunk_size 200)
payload = bytes((i % 256) for i in range(1000))

while True:
    if not edge.got_file():
        # No chunk_size: the node cuts the file at whatever its frames can carry in the
        # posture it is speaking, which is the only place that number is known.
        edge.set_file(AlLoRa_File(name="hello.bin",
                                  content=bytearray(payload)))
        print("file set:", edge.file.get_name())
    # serves the file to whoever asks, secure handshake included; returns once fully sent.
    edge.send_file()
    print("file delivered")

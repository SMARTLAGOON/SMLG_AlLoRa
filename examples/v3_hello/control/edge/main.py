# v3 control: the Edge (the node that HAS the data, serves it, and obeys signed commands).
# Flash the AlLoRa firmware, then put this + LoRa.json + control_root.key on the device.
#
# Same transfer as ../../secure, with one thing added: a control root. From the moment a node
# holds one it refuses unsigned commands, because a signature that an unsigned frame could
# bypass would be protecting nothing.
import gc
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Control.Node_Control_Actuator import Node_Control_Actuator
from AlLoRa.DataSinks.Control_Root_DataSink import Control_Root_DataSink

gc.enable()

edge = Edge(SX127x_connector(), config_file="LoRa.json")
print("EDGE ready | MAC:", edge.MAC, "| session:", edge.session_id)

# This example needs both halves of the provisioning, and the two ways it can be half done are
# worth separating, because they fail differently.
if edge.control_root is None:
    raise SystemExit("This node has no control root. Add control_root_file to LoRa.json and "
                     "copy the verifying half onto the board, or load ../../secure instead.")
if edge.device_id is None:
    # A rooted node on an open link: the half-provisioned state of a fleet whose nodes already
    # carry the root while their config has not been moved to secure yet. No verify gate can be
    # built here, and that is a property of the design rather than a gap: a signed artifact is
    # addressed to a 32-byte device_id, and an open node has no identity to be addressed by. So
    # this node can only ever do the refusing half of holding a root.
    #
    # The refusing half still works, and it is the useful half: with nothing attached, every
    # unsigned in-band command is declined and the Hub reports that refusal rather than reading
    # it as silence. Halting here instead is the honest answer, because a node left running in
    # this state looks provisioned and can never accept a command.
    raise SystemExit("This node holds a control root but has no identity, so it can refuse "
                     "unsigned commands and never verify a signed one. Set security_mode "
                     "'secure' plus identity_file in LoRa.json to finish provisioning.")

print("EDGE device_id (register this on the Hub):", edge.device_id.hex())

# What acts on a verified command (an RF-config change, a reset). It queues the effect rather
# than performing it, so the node applies it only once the acknowledgement is on the air:
# switching the radio first would cost the Hub its confirmation.
actuator = Node_Control_Actuator(edge)

# The downlink sink IS the verify gate: only what it verifies against the provisioned root
# reaches the actuator, and unsigned in-band commands are refused from here on.
edge.data_sink = Control_Root_DataSink(
    control_root=edge.control_root,
    device_id=edge.device_id,
    actuator=actuator,
    # Remembers the highest command number this node has accepted, so a command the Hub already
    # sent cannot be recorded off the air and replayed back at it later. The node resolves where
    # that file goes, from the same config key the Hub reads, so both ends agree by default and
    # move together if a deployment puts it somewhere else.
    counter_file=edge.control_counter_file)
print("EDGE verifying signed control against the provisioned root")

# a 1000-byte test file (~5 chunks at chunk_size 200)
payload = bytes((i % 256) for i in range(1000))

while True:
    if not edge.got_file():
        # No chunk_size: the node cuts the file at whatever its frames can carry in the
        # posture it is speaking, which is the only place that number is known.
        edge.set_file(AlLoRa_File(name="hello.bin",
                                  content=bytearray(payload)))
        print("file set:", edge.file.get_name())
    # serves the file to whoever asks, secure handshake included; a signed artifact rides one of
    # these visits, and the node decides on it afterwards, on its own.
    edge.send_file()
    print("file delivered")

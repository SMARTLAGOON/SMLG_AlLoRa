# v3 hello-world: the Edge (the node that HAS the data and serves it).
# Flash the AlLoRa firmware, then put this + LoRa.json on the device as main.py + LoRa.json.
import gc
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

gc.enable()

edge = Edge(SX127x_connector(), config_file="LoRa.json")
print("EDGE ready | MAC:", edge.MAC, "| session:", edge.session_id)
# In secure mode the Edge has a crypto identity: register THIS device_id on the Hub
# (its first 4 bytes address first contact; its first byte seeds the session id). It is stable
# across reboots as long as identity_file is set in LoRa.json.
if edge.device_id is not None:
    print("EDGE device_id (register this on the Hub):", edge.device_id.hex())

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

# v3 hello-world — Source (the node that HAS the data / responds).
# Flash the AlLoRa firmware, then put this + LoRa.json on the device as main.py + LoRa.json.
import gc
from AlLoRa.Nodes.Source import Source
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

gc.enable()

source = Source(SX127x_connector(), config_file="LoRa.json")
print("SOURCE ready | MAC:", source.MAC, "| session:", source.session_id)
# In secure mode the Source has a crypto identity: register THIS device_id on the Collector
# (its first 4 bytes address first contact; its first byte seeds the session id). It is stable
# across reboots as long as identity_file is set in LoRa.json.
if source.device_id is not None:
    print("SOURCE device_id (register this on the Collector):", source.device_id.hex())

# a 1000-byte test file (~5 chunks at chunk_size 200)
payload = bytes((i % 256) for i in range(1000))

while True:
    if not source.got_file():
        source.set_file(AlLoRa_File(name="hello.bin",
                                    content=bytearray(payload),
                                    chunk_size=source.get_chunk_size()))
        print("file set:", source.file.get_name())
    # serves the file to whoever asks (handles OK -> metadata -> chunks -> final OK, incl. the
    # secure handshake if security_mode is "secure"); returns once fully sent, then re-arms.
    source.send_file()
    print("file delivered")

# v3 hello-world — Collector (the Requester preset: DRIVES the transfer, pulls the file).
# Flash the AlLoRa firmware, then put this + LoRa.json on the *other* device as main.py + LoRa.json.
import gc
import time
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

gc.enable()

collector = Requester(SX127x_connector(), config_file="LoRa.json")
print("COLLECTOR ready | MAC:", collector.MAC)

# session_id MUST match the Source's LoRa.json session_id (v3 addresses by session, not MAC).
# mac_address is just a label + the save-folder name here; set it to the Source's MAC (which the
# Source prints on boot) or leave it as-is.
endpoint = Digital_Endpoint(name="src", mac_address="00000000", active=True, session_id=42)

print("listening for the source (session 42)...")
while True:
    # runs the whole pull (secure handshake first if security_mode is "secure"), prints the file,
    # and saves it under Results/<mac>/ on flash; one_file=True returns after one complete file.
    collector.listen_to_endpoint(endpoint, listening_time=60,
                                 print_file=True, save_file=True, one_file=True)
    gc.collect()
    time.sleep(2)

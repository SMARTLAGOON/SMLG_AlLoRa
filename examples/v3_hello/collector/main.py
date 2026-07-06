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

# OPEN mode: v3 data transfer addresses by session id, so session_id must match the Source's
# LoRa.json. mac_address is only a label + the save-folder name here.
endpoint = Digital_Endpoint(name="src", mac_address="9eeff0dc", active=True, session_id=42)

# SECURE mode: register the Source by its device_id (it prints it on boot). First contact is
# addressed by device_id[:4] — no MAC on the wire — and the session id derives from it, so no
# session_id is needed. Swap the line above for:
#   endpoint = Digital_Endpoint(name="src", device_id="<paste the Source's device_id>", active=True)

print("listening for the source...")
while True:
    # runs the whole pull (secure handshake first if security_mode is "secure"), prints the file,
    # and saves it under Results/<mac>/ on flash; one_file=True returns after one complete file.
    collector.listen_to_endpoint(endpoint, listening_time=60,
                                 print_file=True, save_file=True, one_file=True)
    gc.collect()
    time.sleep(2)

# v3 hello-world, OPEN mode: the Hub (DRIVES the transfer, pulls the file).
# Flash the AlLoRa firmware, then put this + LoRa.json on the *other* device as main.py +
# LoRa.json.
import gc
import time
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

gc.enable()

hub = Hub(SX127x_connector(), config_file="LoRa.json")
print("HUB ready | MAC:", hub.MAC)

# v3 data transfer addresses by session id, so session_id must match the Edge's LoRa.json.
# mac_address is only a label and the save-folder name here. In ../../secure neither line is
# needed: both ends derive the session id from the Edge's identity.
#
# No radio settings here on purpose: an endpoint that states none is polled on this Hub's own
# LoRa.json, so changing sf in both LoRa.json files is all it takes to move the pair to another
# spreading factor. Give it a `config={... "connector": {...}}` only to poll an Edge on settings
# that differ from this node's.
endpoint = Digital_Endpoint(name="src", mac_address="9eeff0dc", active=True, session_id=42)

print("listening for the edge...")
while True:
    # runs the whole pull, prints the file, and saves it under Results/<label>/ on flash;
    # one_file=True returns after one complete file.
    hub.listen_to_endpoint(endpoint, listening_time=60,
                           print_file=True, save_file=True, one_file=True)
    gc.collect()
    time.sleep(2)

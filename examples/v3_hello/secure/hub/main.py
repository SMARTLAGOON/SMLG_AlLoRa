# v3 hello-world, SECURE mode: the Hub (DRIVES the transfer, pulls the file).
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

# Register the Edge by the device_id it prints on boot. Paste that value here.
#
# There is no session_id to keep in sync here, and no MAC: first contact is addressed by
# device_id[:4] and both ends derive the same session id from the same identity. One value
# registers a node instead of two, and device_id[:4] is also what names the save folder.
EDGE_DEVICE_ID = "<paste the Edge's device_id>"

# Checked here because the raw failure is unhelpful: an unedited placeholder reaches
# bytes.fromhex and comes back as a hex-parsing error, which reads like a corrupt value rather
# than one nobody has filled in yet.
if EDGE_DEVICE_ID.startswith("<"):
    raise SystemExit("Set EDGE_DEVICE_ID to the device_id the Edge prints on boot.")

endpoint = Digital_Endpoint(name="src", device_id=EDGE_DEVICE_ID, active=True)

print("listening for the edge...")
while True:
    # runs the whole pull, secure handshake first, then prints the file and saves it under
    # Results/<label>/ on flash; one_file=True returns after one complete file.
    hub.listen_to_endpoint(endpoint, listening_time=60,
                           print_file=True, save_file=True, one_file=True)
    gc.collect()
    time.sleep(2)

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

# session_id MUST match the Source's LoRa.json session_id (v3 data transfer addresses by session).
#
# mac_address: in OPEN mode this is only a label + the save-folder name. In SECURE mode it MUST be
# the Source's real MAC: the pre-session ECDH handshake has no session id yet, so it is addressed
# by MAC, and the Source only answers a handshake aimed at its own MAC. The Source prints it on
# boot as  "S : xxxxxxxx"  (the last 8 hex chars of its wifi MAC). Set this to that value.
endpoint = Digital_Endpoint(name="src", mac_address="9eeff0dc", active=True, session_id=42)

print("listening for the source (session 42)...")
while True:
    # runs the whole pull (secure handshake first if security_mode is "secure"), prints the file,
    # and saves it under Results/<mac>/ on flash; one_file=True returns after one complete file.
    collector.listen_to_endpoint(endpoint, listening_time=60,
                                 print_file=True, save_file=True, one_file=True)
    gc.collect()
    time.sleep(2)

# v3 control: the Hub moves the pair to a new radio setting, over the air.
# Run this once on the Hub, in place of ../../main.py, to send the retune and watch it happen.
# It registers its Edge here rather than from Nodes.json, so paste the Edge's device_id below.
# How the command travels depends only on how the nodes were provisioned (see the README).
# Here they hold a control root, so the command is signed.
import gc
import time
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

# Where to move the pair. `trial` is how many seconds the Edge waits for a transfer on the new
# settings before rolling back, so make it longer than one round of visits.
NEW_CONFIG = {"sf": 9, "trial": 300}

gc.enable()

hub = Hub(SX127x_connector(), config_file="AlLoRa.json")
print("HUB ready | MAC:", hub.MAC)

# Without the signing key, a provisioned Edge refuses every command and it looks like a bad link.
if hub.control_root is None:
    raise SystemExit("This Hub has no control root, so it cannot sign a command. Add "
                     "control_root_file to AlLoRa.json and copy the signing half onto the "
                     "board.")

# The device_id the Edge prints on boot. Signed commands are addressed to it.
EDGE_DEVICE_ID = "<paste the Edge's device_id>"

if EDGE_DEVICE_ID.startswith("<"):
    raise SystemExit("Set EDGE_DEVICE_ID to the device_id the Edge prints on boot.")

endpoint = Digital_Endpoint(name="src", device_id=EDGE_DEVICE_ID, active=True)

# 1. One complete pull first, so a failure afterwards points at the new settings, not the link.
print("pulling one file on the current configuration...")
hub.listen_to_endpoint(endpoint, listening_time=60, save_file=True, one_file=True)

# 2. One call moves both ends: the Edge, and this Hub after it. The answer is one of four words.
print("asking for", NEW_CONFIG)
outcome = hub.ask_change_rf(endpoint, NEW_CONFIG)
if outcome == Hub.ACCEPTED:
    print("acknowledged: the Edge is now on trial with the new configuration")
elif outcome == Hub.PENDING:
    # The expected answer here: the signed command reaches the Edge on one of the pulls below.
    print("artifact queued: it is delivered on one of the pulls below, and the probe below "
          "is what says whether the Edge took it")
elif outcome == Hub.REFUSED:
    # Alive, heard it, said no: usually the Edge holds another fleet's control root.
    print("refused: the Edge is there and declined the command; check its provisioning")
else:
    print("unreachable: nothing answered at all, so the command was never considered")

# 3. Keep polling: a transfer on the new settings ends the Edge's trial, and if none gets
#    through, the Edge rolls back and these visits find it on the old settings.
while True:
    hub.listen_to_endpoint(endpoint, listening_time=60, save_file=True, one_file=True)
    fallback = hub.endpoint_trial_old(endpoint)
    if fallback is None:
        print("settled on sf", endpoint.sf)
    else:
        print("trial running: polling sf", endpoint.sf, "| fallback sf", fallback[1])
    gc.collect()
    time.sleep(2)

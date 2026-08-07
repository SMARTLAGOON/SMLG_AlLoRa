# v3 control: the Hub moves the pair onto a new radio configuration, over the air.
#
# Same two boards as the hello-world, same Edge. Put this on the Hub board as main.py instead
# of hub/main.py. Nothing here selects how the command travels: that follows from what the two
# nodes were provisioned with, which is the whole of the setup (see the README).
import gc
import time
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

# Where to move the pair. `trial` is how many seconds the Edge holds this provisionally before
# rolling itself back, and it wants to be longer than one poll rotation. What commits the trial
# is the peer asking for a chunk later than the last one served, which is the first evidence a
# full-payload frame was demodulated on the new settings; a bare reachability poll is not
# evidence and does not commit. So the window has to be long enough for a visit to reach that
# point, or it restores a configuration that was working.
NEW_CONFIG = {"sf": 9, "trial": 300}

gc.enable()

hub = Hub(SX127x_connector(), config_file="LoRa.json")
print("HUB ready | MAC:", hub.MAC)

# OPEN mode, exactly as in hub/main.py: session_id must match the Edge's LoRa.json.
endpoint = Digital_Endpoint(name="src", mac_address="9eeff0dc", active=True, session_id=42)

# SECURE mode: register the Edge by the device_id it prints on boot, and give both boards a
# control_root_file. Swap the line above for:
#   endpoint = Digital_Endpoint(name="src", device_id="<paste the Edge's device_id>", active=True)

print("control transport:", "signed artifact" if hub.control_root is not None else "in band")

# 1. A safe boundary. One complete pull first, so the link is known good before it is moved and
#    a failure afterwards means the new configuration, not the antenna. Commanding a retune part
#    way through a transfer would also move the radio out from under it.
print("pulling one file on the current configuration...")
hub.listen_to_endpoint(endpoint, listening_time=60, save_file=True, one_file=True)

# 2. One call moves both ends. The Edge applies the change after its acknowledgement is on the
#    air, and this Hub follows it onto the same settings: a reconfiguration where only the far
#    end moves is not a partial success, it is an endpoint this Hub can no longer hear.
#
#    True means two different things and it is worth knowing which. In band the Edge has
#    already acknowledged and applied the change by the time this returns. Signed, the artifact
#    is only queued: it is handed over on one of the pulls below, and nothing has moved yet.
print("asking for", NEW_CONFIG)
accepted = hub.ask_change_rf(endpoint, NEW_CONFIG)
if not accepted:
    print("the command did not land; leaving the pair where it is")
elif hub.control_root is not None:
    print("artifact queued: it is delivered on one of the pulls below")
else:
    print("acknowledged: the Edge is now on trial with the new configuration")

# 3. Keep polling. This is not idling: on the new configuration a completed transfer is what
#    commits the Edge's trial, and if the new configuration cannot carry one, the Edge restores
#    itself and these visits are what find it again on the old one.
while True:
    hub.listen_to_endpoint(endpoint, listening_time=60, save_file=True, one_file=True)
    fallback = hub.endpoint_trial_old(endpoint)
    if fallback is None:
        print("settled on sf", endpoint.sf)
    else:
        print("trial running: polling sf", endpoint.sf, "| fallback sf", fallback[1])
    gc.collect()
    time.sleep(2)

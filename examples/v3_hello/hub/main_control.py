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
#    The answer is one of four words, and each one sends you somewhere different. They are worth
#    reading in full, because two of them used to be the same word and the wrong one cost a trip
#    to a site: a node that heard the command and declined it looked exactly like a dead antenna.
print("asking for", NEW_CONFIG)
outcome = hub.ask_change_rf(endpoint, NEW_CONFIG)
if outcome == Hub.ACCEPTED:
    print("acknowledged: the Edge is now on trial with the new configuration")
elif outcome == Hub.PENDING:
    print("artifact queued: it is delivered on one of the pulls below, and the probe below "
          "is what says whether the Edge took it")
elif outcome == Hub.REFUSED:
    # It answered a poll, so it is alive and listening. It heard the command and said no,
    # which for an unsigned command means the node holds a control root and wants a signed
    # one. Check what this Hub was provisioned with, not the antenna.
    print("refused: the Edge is there and declined the command; check its provisioning")
else:
    print("unreachable: nothing answered at all, so the command was never considered")

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

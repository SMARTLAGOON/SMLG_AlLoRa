# The Hub, provisioned by the AlLoRa wizard.
#
# The one thing that differs from examples/v3_hello: the Edges this Hub polls are registered in
# Nodes.json rather than pasted into this file as a constant. That is the file
# `Hub.add_digital_endpoints` already reads and the file the Hub already writes settled radio
# settings back into, and a backend can read and edit a file where it cannot edit a constant.
#
# Which posture this node speaks is decided entirely by LoRa.json, not by anything here.
import gc

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.__RADIO_MODULE__ import __RADIO_CLASS__

gc.enable()

hub = Hub(__RADIO_CLASS__(), config_file="LoRa.json", nodes_file="Nodes.json")
print("HUB ready | MAC:", hub.MAC, "| mode:", hub.security_mode,
      "| endpoints:", len(hub.digital_endpoints))

# An empty roster is the one failure that looks like a working Hub: it boots, it prints, and it
# polls nobody. Registration is the whole of the setup on this side, so say so and stop.
if not hub.digital_endpoints:
    raise SystemExit("Nodes.json registered no active endpoint. The wizard writes that file; "
                     "re-run the hub step, or set \"active\": true on an entry.")

for endpoint in hub.digital_endpoints:
    print("  polling", endpoint.get_name(), "as", endpoint.get_label(),
          "every", endpoint.asking_frequency, "s")

hub.run(save_files=True)

# One WiFi_adapter serves both modes; "mode" in the config's adapter block picks which.
# "client" is the default, so this board joins an existing network instead of hosting one.

from AlLoRa.Adapters.WiFi_adapter import WiFi_adapter
from AlLoRa.Connectors.LoPy4_connector import LoPy4_connector

if __name__ == "__main__":
	lora_adapter = WiFi_adapter(LoPy4_connector())
	lora_adapter.run()

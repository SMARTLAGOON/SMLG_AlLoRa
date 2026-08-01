from AlLoRa.Adapters.WiFi_adapter import WiFi_adapter
from AlLoRa.Connectors.LoPy4_connector import LoPy4_connector

if __name__ == "__main__":
	lora_adapter = WiFi_adapter(LoPy4_connector(), "LoRaWiFi.json")
	lora_adapter.run()

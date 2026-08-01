from AlLoRa.Adapters.WiFi_adapter import WiFi_adapter
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

def run():
	lora_adapter = WiFi_adapter(SX127x_connector(), "LoRaWiFi.json")
	lora_adapter.run()

run()

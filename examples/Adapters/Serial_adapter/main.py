# Main for the Adapter on the Collector side of AlLoRa
# HW: TTGO LoRa 32

from AlLoRa.Adapters.Serial_adapter import Serial_adapter
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

if __name__ == "__main__":

	# The config file is not optional and has no default: Adapter only boots, and so only
	# builds its Link, when it is given one. Without it this runs forever with no link.
	lora_adapter = Serial_adapter(SX127x_connector(), "LoRa.json")
	lora_adapter.run()

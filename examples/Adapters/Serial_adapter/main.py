# Main for the Adapter on the Collector side of AlLoRa
# HW: TTGO LoRa 32

from AlLoRa.Adapters.Serial_adapter import Serial_adapter
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

if __name__ == "__main__":

	lora_adapter = Serial_adapter(SX127x_connector())
	lora_adapter.run()

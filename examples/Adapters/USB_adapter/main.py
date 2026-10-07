# The Adapter for a Hub on a host. This board is the host's LoRa radio, over one USB cable.
# Tested on the LilyGo T3S3. Other native-USB boards (ESP32-S3, ESP32-C3, RP2040) run the same code.
# On the host, set the Hub's "serial_port" to where this board appears (see ../README.md).
# The cable is also this board's console, so debug output is off. Set "log": "file" to keep it.

from AlLoRa.Adapters.USB_adapter import USB_adapter
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

if __name__ == "__main__":

	lora_adapter = USB_adapter(SX127x_connector(), "LoRa.json")
	lora_adapter.run()

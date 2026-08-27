# Main for the Adapter on the Hub side of AlLoRa, reached over USB.
# HW: LilyGo T3S3. The same code is what any other native-USB board needs (ESP32-S3,
# ESP32-C3, RP2040), but the T3S3 is the one this has been run on.
#
# Plug this board into a Raspberry Pi (or any host) with one USB cable and it is that host's
# LoRa modem. Nothing else is wired: no pins, no baud to agree on. The host runs the Hub and
# holds the keys; this board only works the radio.
#
# On the host, point the Hub's connector at the port the board turned up on:
#     "serial_port": "/dev/ttyACM0"        # a Raspberry Pi
#     "serial_port": "/dev/cu.usbmodem101" # macOS
#
# Note this board's own console is the cable, so it cannot also print to it. Debug output is
# off; set "log": "file" in LoRa.json for a bench session.

from AlLoRa.Adapters.USB_adapter import USB_adapter
from AlLoRa.Connectors.SX127x_connector import SX127x_connector

if __name__ == "__main__":

	lora_adapter = USB_adapter(SX127x_connector(), "LoRa.json")
	lora_adapter.run()

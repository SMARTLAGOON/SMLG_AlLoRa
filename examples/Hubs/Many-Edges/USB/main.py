# A Hub on a host (a Raspberry Pi, a laptop) whose radio is a board on one USB cable.
# The host runs the Hub and holds the keys. The board runs examples/Adapters/USB_adapter.
# Unlike the Serial/ example, it needs no pins, no baud rate and no reset wire.
# Resetting the board also resets its USB port, which may come back on another path.
# Serial_connector finds it again: see `recover_link`.

from serial.tools import list_ports

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.Split.Serial_connector import Serial_connector, usb_reset


# The adapter's MAC, the four bytes its radio answers to. Set it to your board's.
ADAPTER_MAC = "9eeff0e0"


# Finds the adapter's USB port by the serial number the ESP32 builds from its MAC: lowercase
# "9c139eeff0e0cb3f" when running, "9C:13:9E:EF:F0:E0" in the ROM loader, so only hex digits are
# compared. The connector then asks the board its MAC to confirm it found the right one.
def _hex_digits(text):
    return "".join(c for c in text.lower() if c in "0123456789abcdef")


def find_adapter():
    wanted = _hex_digits(ADAPTER_MAC)
    for port in list_ports.comports():
        if port.serial_number and wanted in _hex_digits(port.serial_number):
            return port.device
    raise IOError("No board with MAC {} on the USB bus".format(ADAPTER_MAC))


def reset_adapter():
    # Stands in for the reset wire. A board that has dropped off USB still needs a power cycle.
    usb_reset(find_adapter())


if __name__ == "__main__":
    connector = Serial_connector(reset_function=reset_adapter, port_resolver=find_adapter)
    allora_hub = Hub(connector, config_file="LoRa.json", debug_hops=False)
    allora_hub.run(print_file_content=True, save_files=True)

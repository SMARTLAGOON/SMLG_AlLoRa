# A Hub on a host (a Raspberry Pi, a laptop) whose radio is a board on the other end of one
# USB cable. The host runs the engine, the endpoint roster, the keys and the sessions; the
# board runs `examples/Adapters/USB_adapter` and works the radio and nothing else.
#
# The sibling `Serial/` example is the same Hub over a Pi's own GPIO UART, with a wire to the
# board's RST pin. This one needs neither: no pins, no baud to agree on, no reset wire, and it
# runs on any host with a USB socket rather than only on a Pi.
#
# What the cable costs you is that a reset now takes the port with it. Over GPIO the port
# belongs to the Pi, so resetting the board leaves the file descriptor valid. Over USB the
# serial device *is* the board: reset it and it re-enumerates, the descriptor dies, and it may
# come back on a different path. `Serial_connector` handles all three of those now, which is
# what makes this example a drop-in rather than a downgrade. See `recover_link`.

from serial.tools import list_ports

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.Serial_connector import Serial_connector, usb_reset


# The adapter's MAC: the same four bytes the radio answers to on the air, and the same ones the
# provisioning toolkit files the board under. Set it to your board's.
ADAPTER_MAC = "9eeff0e0"


# Which device node the adapter is on *right now*. A board that reboots comes back on whatever
# node the host next hands out, so anything holding a fixed path is one reboot away from
# talking to the wrong device, or to nothing.
#
# The lookup works because an ESP32 publishes a USB serial number built from its MAC, so the
# board is identifiable from the host before a word has been said over the tunnel: no esptool,
# no REPL round trip. Measured on the bench on 2026-09-02, a T3-S3 whose radio answers to
# 9eeff0e0 enumerates as:
#
#     9c139eeff0e0cb3f      running MicroPython
#     9C:13:9E:EF:F0:E0     sitting in the ROM loader
#
# Same board, same six-byte MAC, two different renderings of it. That is why the comparison
# strips everything that is not a hex digit and lowercases what is left, rather than testing
# the string as given. A board is in the second state precisely when it has just been reset,
# which is to say exactly when a recovery needs to find it.
#
# It is still only a guess, and the connector checks it: after every reopen it asks the board
# its radio MAC over the tunnel and compares. A reopen that landed on some other device fails
# rather than quietly carrying on.
def _hex_digits(text):
    return "".join(c for c in text.lower() if c in "0123456789abcdef")


def find_adapter():
    wanted = _hex_digits(ADAPTER_MAC)
    for port in list_ports.comports():
        if port.serial_number and wanted in _hex_digits(port.serial_number):
            return port.device
    raise IOError("No board with MAC {} on the USB bus".format(ADAPTER_MAC))


def reset_adapter():
    # The stand-in for the RST wire the GPIO example pulses. Reaches every failure where the
    # chip is still on the bus; a board that has left the bus (deep sleep with USB down, a
    # brown-out) still needs a wire or a power cycle.
    usb_reset(find_adapter())


if __name__ == "__main__":
    connector = Serial_connector(reset_function=reset_adapter, port_resolver=find_adapter)
    allora_hub = Hub(connector, config_file="LoRa.json", debug_hops=False)
    allora_hub.run(print_file_content=True, save_files=True)

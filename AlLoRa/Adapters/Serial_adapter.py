"""An Adapter that talks to the logic board over a UART cable.

The UART settings come from the `adapter` block of the config file.
Usage: `Serial_adapter(SX127x_connector()).run()`.
"""
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Links.Serial_link import Serial_link


class Serial_adapter(Adapter):

    def setup_link(self, config):
        self.link = Serial_link.bridge(
            uartid=config.get('uartid', 0),
            baud=config.get('baud', 9600),
            tx=config.get('tx', None),
            rx=config.get('rx', None),
            bits=config.get('bits', 8),
            parity=config.get('parity', None),
            stop=config.get('stop', 1),
            timeout=config.get('timeout_char', config.get('timeout', 800)))

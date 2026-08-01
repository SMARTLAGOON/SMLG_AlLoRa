"""Serial_adapter: a serial tunnel's bridge board.

Thin over Adapter: the bridge holds the radio but no protocol logic. It reads a transport-verb
request off a UART, runs that verb on its real radio Connector, and writes the result back,
never parsing the LoRa wire, never holding a session key. All of that lives in Adapter; this
only builds the concrete Serial_link bridge from the config file's link block. The pre-split
bridge re-parsed the frame with a `S&W:`/`Listen:` string protocol, which is why it could only
ever serve v2; the split serves v2 / v3-open / v3-secure alike.

    adapter = Serial_adapter(SX127x_connector())
    adapter.run()
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

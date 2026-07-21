"""Serial_interface: a serial tunnel's bridge (Adapter) half.

Thin over Tunnel_interface: the Adapter holds the radio but no protocol logic. It reads a
transport-verb request off a UART, runs that verb on its real radio Connector, and writes the
result back, never parsing the LoRa wire, never holding a session key. All of that lives in
Tunnel_interface; this only builds the concrete Serial_link bridge from the interface config.
The old file re-parsed the frame with a `S&W:`/`Listen:` string protocol, which is why the
bridge could only ever serve v2; the split serves v2 / v3-open / v3-secure alike.

The `Serial_Interface` name is kept so the Serial Adapter example imports it unchanged.
"""
from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Links.Serial_link import Serial_link


class Serial_Interface(Tunnel_interface):

    def __init__(self):
        super().__init__()

    def setup(self, connector, debug, config):
        super().setup(connector, debug, config)
        cfg = config or {}
        self.link = Serial_link.bridge(
            uartid=cfg.get('uartid', 0),
            baud=cfg.get('baud', 9600),
            tx=cfg.get('tx', None),
            rx=cfg.get('rx', None),
            bits=cfg.get('bits', 8),
            parity=cfg.get('parity', None),
            stop=cfg.get('stop', 1),
            timeout=cfg.get('timeout_char', cfg.get('timeout', 800)))

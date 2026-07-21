"""Serial_connector: a serial tunnel's logic-holder half.

Thin over Tunnel_connector: it is a split Connector whose transport verbs cross a UART to a
bridge (Adapter) running the radio. All the tunnel logic (exchange-routed matching, D↓/td↑
pacing, opaque wire so v2/v3-open/v3-secure all cross unchanged) lives in Tunnel_connector;
this only builds the concrete Serial_link client from the config and keeps the old constructor
so existing Gateway examples import it unchanged. It replaces the previous ad-hoc
`S&W:`/`ACK:`/`Listen:` string protocol, which re-parsed the frame on the bridge and so only
ever spoke v2.
"""
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Links.Serial_link import Serial_link


class Serial_connector(Tunnel_connector):

    def __init__(self, reset_function=None, rpc_timeout=20, link_margin=5):
        super().__init__(rpc_timeout=rpc_timeout, link_margin=link_margin)
        # Kept for API compatibility with the old connector; the Serial_link owns retries now.
        self.reset_function = reset_function

    def config(self, config_json):
        if config_json:
            self.link = Serial_link.client(
                config_json.get('serial_port', "/dev/ttyAMA3"),
                baud=config_json.get('baud', 9600),
                timeout=config_json.get('timeout', 1))
        super().config(config_json)

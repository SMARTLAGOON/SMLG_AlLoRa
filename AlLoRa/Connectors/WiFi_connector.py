"""WiFi_connector: a WiFi tunnel's logic-holder half.

Thin over Tunnel_connector: a split Connector whose transport verbs cross an HTTP link to a
bridge (Adapter) running the radio. The tunnel logic lives in Tunnel_connector; this only
builds the concrete WiFi_link client from the config and keeps the old constructor so existing
Gateway examples import it unchanged. It replaces the previous JSON-command HTTP protocol,
which re-parsed the frame on the bridge and so only ever spoke v2.
"""
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Links.WiFi_link import WiFi_link


class WiFi_connector(Tunnel_connector):

    def __init__(self, rpc_timeout=20, link_margin=5):
        super().__init__(rpc_timeout=rpc_timeout, link_margin=link_margin)

    def config(self, config_json):
        if config_json:
            self.link = WiFi_link.client(
                host=config_json.get('requester_api_host', "192.168.4.1"),
                port=config_json.get('requester_api_port', 80),
                timeout=config_json.get('socket_timeout', 20),
                recv_size=config_json.get('socket_recv_size', 4096))
        super().config(config_json)

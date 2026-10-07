"""A Tunnel_connector that reaches the radio board over HTTP on WiFi."""
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

"""WiFi_adapter: a WiFi tunnel's bridge board.

Thin over Adapter: the bridge holds the radio but no protocol logic. It brings up the network
(AP hotspot or STA client), then reads transport-verb requests off an HTTP link, runs each verb
on its real radio Connector, and writes the result back, never parsing the LoRa wire, never
holding a session key. The verb pumping and dispatch live in Adapter; this owns only the WiFi
bring-up and building the concrete WiFi_link bridge over it. The pre-split bridge re-parsed the
frame with a JSON-command protocol, which is why it could only ever serve v2; the split serves
v2 / v3-open / v3-secure alike.

    adapter = WiFi_adapter(SX127x_connector())
    adapter.run()

The bring-up here and the byte transport in WiFi_link are deliberately separate: this gets the
network up, the link moves bytes over it. The MicroPython network module is imported inside
init_wifi rather than at module scope, so the file itself loads anywhere (which is what lets
the config-carrying behaviour below be covered off-device).
"""
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Links.WiFi_link import WiFi_link
from AlLoRa.utils.time_utils import sleep
from AlLoRa.utils.debug_utils import print


class WiFi_adapter(Adapter):

    def setup_link(self, config):
        self.mode = config.get('mode', 'client')          # 'client' or 'hotspot'
        self.ssid = config.get('ssid', "AlLoRa-Adapter")
        self.psw = config.get('psw', "AlLoRaWiFi")
        self.host = config.get('host', "192.168.4.1")
        self.port = config.get('port', 80)
        self.ip = config.get('ip', '192.168.0.16')
        self.subnet_mask = config.get('subnet_mask', '255.255.255.0')
        self.gateway = config.get('gateway', '192.168.1.10')
        self.DNS_server = config.get('DNS_server', '8.8.8.8')
        self.wlan = None

        self.init_wifi()
        sleep(1)
        # The network is up; the link owns the byte transport (bind/accept/recv/send) over it.
        self.link = WiFi_link.bridge(host=self.host, port=self.port)

    def init_wifi(self):
        # Pycom boards drive WLAN through their own API; everything else uses the standard
        # MicroPython network module. Imported here, not at module scope, so the file loads on
        # a host that has neither.
        try:
            import pycom
            from network import WLAN
            pycom_board = True
        except ImportError:
            import network
            pycom_board = False

        if self.mode == 'hotspot':
            if pycom_board:
                self.wlan = WLAN()
                self.wlan.init(mode=WLAN.AP, ssid=self.ssid, auth=(WLAN.WPA2, self.psw))
            else:
                self.wlan = network.WLAN(network.AP_IF)
                self.wlan.active(True)
                self.wlan.config(essid=self.ssid, password=self.psw)
        elif self.mode == 'client':
            if pycom_board:
                self.wlan = WLAN()
                self.wlan.init(mode=WLAN.STA)
                self.connect()
            else:
                self.wlan = network.WLAN(network.STA_IF)
                self.wlan.active(True)
                self.connect()

    def connect(self):
        while not self.wlan.isconnected():
            try:
                self.wlan.connect(self.ssid, self.psw)
                if self.debug:
                    print("Connecting to WiFi...", end='')
                while not self.wlan.isconnected():
                    sleep(1)
                    if self.debug:
                        print(".", end='')
            except Exception as e:
                if self.debug:
                    print("Exception connecting to WiFi:", e)

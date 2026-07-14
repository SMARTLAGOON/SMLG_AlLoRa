"""WiFi_interface — a WiFi tunnel's bridge (Adapter) half.

Thin over Tunnel_interface: the Adapter holds the radio but no protocol logic. It brings up
the network (AP hotspot or STA client), then reads transport-verb requests off an HTTP link,
runs each verb on its real radio Connector, and writes the result back — never parsing the
LoRa wire, never holding a session key. The verb pumping + dispatch live in Tunnel_interface;
this owns only the WiFi bring-up and building the concrete WiFi_link bridge over it. The old
file re-parsed the frame with a JSON-command protocol, which is why the bridge could only ever
serve v2; the split serves v2 / v3-open / v3-secure alike.

The `WiFi_Interface` name is kept so the WiFi Adapter examples import it unchanged. This module
is device-only (MicroPython network / usocket); it is never imported on the CPython test path.
"""
from utime import sleep

from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Links.WiFi_link import WiFi_link
from AlLoRa.utils.debug_utils import print

PYCOM = False
try:
    import pycom
    from network import WLAN
    PYCOM = True
except ImportError:
    import network
    PYCOM = False


class WiFi_Interface(Tunnel_interface):

    def __init__(self):
        super().__init__()
        self.wlan = None

    def setup(self, connector, debug, config):
        super().setup(connector, debug, config)
        cfg = config or {}
        self.mode = cfg.get('mode', 'client')          # 'client' or 'hotspot'
        self.ssid = cfg.get('ssid', "AlLoRa-Adapter")
        self.psw = cfg.get('psw', "AlLoRaWiFi")
        self.host = cfg.get('host', "192.168.4.1")
        self.port = cfg.get('port', 80)
        self.ip = cfg.get('ip', '192.168.0.16')
        self.subnet_mask = cfg.get('subnet_mask', '255.255.255.0')
        self.gateway = cfg.get('gateway', '192.168.1.10')
        self.DNS_server = cfg.get('DNS_server', '8.8.8.8')

        self.init_wifi()
        sleep(1)
        # The network is up; the link owns the byte transport (bind/accept/recv/send) over it.
        self.link = WiFi_link.bridge(host=self.host, port=self.port)

    def init_wifi(self):
        if self.mode == 'hotspot':
            if PYCOM:
                self.wlan = WLAN()
                self.wlan.init(mode=WLAN.AP, ssid=self.ssid, auth=(WLAN.WPA2, self.psw))
            else:
                self.wlan = network.WLAN(network.AP_IF)
                self.wlan.active(True)
                self.wlan.config(essid=self.ssid, password=self.psw)
        elif self.mode == 'client':
            if PYCOM:
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

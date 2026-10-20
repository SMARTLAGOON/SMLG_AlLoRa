from time import sleep

from AlLoRa.Protocol.Packet_v2 import Packet_v2
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print
from network import LoRa
import socket
import binascii

import pycom

class LoPy4_connector(Connector):

    def __init__(self):
        super().__init__()
        self.MAC = binascii.hexlify(LoRa().mac()).decode('utf-8')[-8:]

    def config(self, config_json):
        # JSON Example:
        # {
        #     "name": "L",
        #     "frequency": 868,
        #     "sf": 7,
        #     "mesh_mode": false,
        #     "debug": false,
        #     "min_timeout": 0.5,
        #     "max_timeout": 6
        # }
        super().config(config_json)
        self.lora = LoRa(mode=LoRa.LORA, frequency=self.frequency*1000000,
                            region=LoRa.EU868, sf = self.sf)
        self.lora_socket = socket.socket(socket.AF_LORA, socket.SOCK_RAW)

    def set_sf(self, sf):
        if self.sf != sf:
            self.lora.sf(sf)
            self.sf = sf
            if self.debug:
                print("SF Changed to: ", self.sf)

    def get_stats(self):
        stats = self.lora.stats()
        if self.debug:
            print("rx_timestamp {0}, rssi {1}, snr {2}, sftx {3}, sfrx {4}, tx_trials {5}, tx_power {6}, tx_time_on_air {7}, tx_counter {8}, tx_frequency {9}".format(stats[0],
                    stats[1], stats[2], stats[3], stats[4], stats[5], stats[6], stats[7], stats[8], stats[9]))
        return stats

    def get_rssi(self):
        return self.lora.stats()[1]

    def send(self, packet):
        if packet.get_length() <= Connector.MAX_LENGTH_MESSAGE:
            if self.debug:
                print("SEND_PACKET(SF: {}) || packet: {}".format(self.sf, packet.get_content()))
            if packet.get_mesh():
                pycom.rgbled(0xb19cd8) # purple
            else:
                pycom.rgbled(0x007f00) # green
            try:
                self.lora_socket.send(packet.get_content())	#.encode()
                #if self.debug:
                pycom.rgbled(0)        # off
                return True
            except:
                #if self.debug:
                pycom.rgbled(0)        # off
                return False
        else:
            if self.debug:
                print("Error: Packet too big")
            return False

    def recv(self, focus_time=12):
        try:
            self.lora_socket.settimeout(focus_time)
            data = self.lora_socket.recv(Connector.MAX_LENGTH_MESSAGE)
            self.lora_socket.setblocking(False)
            if self.debug:
                self.get_stats()
            return data
        except Exception as e:
            if self.debug:
                print("Error receiving: ", e)

    def send_and_wait_response(self, packet):
        return super().send_and_wait_response(packet)

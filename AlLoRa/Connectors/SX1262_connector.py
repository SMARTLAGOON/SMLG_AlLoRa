import ubinascii
import network

from sx1262 import SX1262

from AlLoRa.Packet import Packet
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print

class SX1262_connector(Connector):
    def __init__(self):
        super().__init__()
        self.lora = None
        try:
            wlan_sta = network.WLAN(network.STA_IF)
            wlan_sta.active(True)
            wlan_mac = wlan_sta.config('mac')
            self.MAC = ubinascii.hexlify(wlan_mac).decode()[-8:]  # Last 8 characters of MAC
            wlan_sta.active(False)
        
        except Exception as e:
            self.MAC = "HELTECV3"
            if self.debug:
                print("Error assigning MAC Connector: ", e, "\nUsing default MAC: ", self.MAC)
            

    def config(self, config_json):
        super().config(config_json)

        # Initialize SX1262 with configuration from JSON or default values (for Heltec LoRa32 V3)
        self.lora = SX1262(spi_bus=config_json.get('spi_bus', 1),
                           clk=config_json.get('clk', 9),
                           mosi=config_json.get('mosi', 10),
                           miso=config_json.get('miso', 11),
                           cs=config_json.get('cs', 8),
                           irq=config_json.get('irq', 14),
                           rst=config_json.get('rst', 12),
                           gpio=config_json.get('gpio', 13))

        # Begin configuration with provided or default parameters
        self.lora.begin(freq=config_json.get('freq', 868.0),
                        bw=config_json.get('bw', 125.0),
                        sf=config_json.get('sf', 7),
                        cr=config_json.get('cr', 5),
                        syncWord=config_json.get('syncWord', 0x34),
                        power=config_json.get('power', 14),
                        preambleLength=config_json.get('preambleLength', 8),
                        implicit=config_json.get('implicit', False),
                        implicitLen=config_json.get('implicitLen', 0xFF),
                        crcOn=config_json.get('crcOn', True),
                        txIq=config_json.get('txIq', False),
                        rxIq=config_json.get('rxIq', False),
                        tcxoVoltage=config_json.get('tcxoVoltage', 1.7),
                        useRegulatorLDO=config_json.get('useRegulatorLDO', False),
                        blocking=config_json.get('blocking', True))


    def set_sf(self, sf):
        if self.sf != sf:
            self.lora.setSpreadingFactor(sf)
            self.sf = sf
            if self.debug:
                print("SF Changed to: ", self.sf)

    def set_bw(self, bw):
        if self.config_parameters.get('bw') != bw:
            self.lora.setBandwidth(bw)
            self.config_parameters['bw'] = bw
            if self.debug:
                print("BW Set to: ", bw)

    def set_cr(self, cr):
        if self.config_parameters.get('cr') != cr:
            self.lora.setCodingRate(cr)
            self.config_parameters['cr'] = cr
            if self.debug:
                print("CR Set to: ", cr)

    def get_rssi(self):
        return self.lora.getRSSI()

    def transmit(self, wire):
        if self.debug:
            print("SEND_PACKET() || packet: {}".format(wire))
        if len(wire) <= Connector.MAX_LENGTH_MESSAGE:
            try:
                self.lora.setBlockingCallback(True)
                self.lora.send(data=wire)
                self.lora.setBlockingCallback(False)
                return True
            except Exception as e:
                if self.debug:
                    print("Send Error: ", e)
                self.lora.setBlockingCallback(False)
                return False
        else:
            if self.debug:
                print("Error: Packet too big")
            return False

    def recv(self, focus_time=12):
        if self.lora:
            try:
                self.lora.setBlockingCallback(True, callback=lambda: None)
                data, state = self.lora.recv(timeout_en=True, timeout_ms=focus_time*1000)
                self.lora.setBlockingCallback(False)
                if state == 0:  # Assuming 0 indicates success
                    return data
                else:
                    if self.debug:
                        print("Receive Error: State ", state)
                    return None
            except Exception as e:
                if self.debug:
                    print("Receive Error: ", e)
                return None




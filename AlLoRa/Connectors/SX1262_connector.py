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

        # Pins, the bus and the chip's own extras come straight out of the connector block: the
        # base class does not know what an SX1262 is, and does not need to. The defaults below
        # are the Heltec LoRa32 V3 map, which is the board this connector was written against.
        self.lora = SX1262(spi_bus=config_json.get('spi_bus', 1),
                           clk=config_json.get('clk', 9),
                           mosi=config_json.get('mosi', 10),
                           miso=config_json.get('miso', 11),
                           cs=config_json.get('cs', 8),
                           irq=config_json.get('irq', 14),
                           rst=config_json.get('rst', 12),
                           gpio=config_json.get('gpio', 13))

        # The RF settings come from the base class, not from a second read of the same file.
        # Reading them again here meant reading them under different names: the schema writes
        # `bandwidth`, `coding_rate` and `tx_power`, and this file asked for `bw`, `cr` and
        # `power`, so a config that named a bandwidth got 125 kHz, one that named a coding rate
        # got 4/5, and one that asked for 20 dBm transmitted at 14. Silently, and only on this
        # radio, which is the same shape of defect as a program that names its own chip.
        self.lora.begin(freq=self.frequency,
                        bw=self.bw,
                        sf=self.sf,
                        cr=self._driver_cr(self.cr),
                        syncWord=config_json.get('syncWord', 0x34),
                        power=self.tx_power,
                        preambleLength=config_json.get('preambleLength', 8),
                        implicit=config_json.get('implicit', False),
                        implicitLen=config_json.get('implicitLen', 0xFF),
                        crcOn=config_json.get('crcOn', True),
                        txIq=config_json.get('txIq', False),
                        rxIq=config_json.get('rxIq', False),
                        tcxoVoltage=config_json.get('tcxoVoltage', 1.7),
                        useRegulatorLDO=config_json.get('useRegulatorLDO', False),
                        blocking=config_json.get('blocking', True))

    @staticmethod
    def _driver_cr(cr):
        """AlLoRa counts coding rates 1..4; this driver wants the denominator, 5..8.

        Handed 1 unconverted, `setCodingRate` returns an invalid-coding-rate code and changes
        nothing, and it returns rather than raises, so every coding-rate change on this radio
        used to be accepted and then not happen.
        """
        return cr + 4 if cr <= 4 else cr

    def set_sf(self, sf):
        if self.sf != sf:
            self.lora.setSpreadingFactor(sf)
            self.sf = sf
            if self.debug:
                print("SF Changed to: ", self.sf)

    def set_bw(self, bw):
        # Track the live value on self.bw (what get_rf_config / backup_config read), not a
        # 'bw' key in config_parameters that the config loader never reads back (it reads
        # 'bandwidth'): otherwise a committed bandwidth change is lost on reboot.
        if self.bw != bw:
            self.lora.setBandwidth(bw)
            self.bw = bw
            if self.debug:
                print("BW Set to: ", bw)

    def set_cr(self, cr):
        if self.cr != cr:
            self.lora.setCodingRate(self._driver_cr(cr))
            self.cr = cr
            if self.debug:
                print("CR Set to: ", cr)

    def set_frequency(self, freq):
        # Without this the base class's no-op ran instead, so a retune to another channel was
        # reported as applied while the radio stayed where it was: both ends then believed they
        # had moved, and only one of them had.
        if self.frequency != freq:
            self.lora.setFrequency(freq)
            self.frequency = freq
            if self.debug:
                print("Frequency Changed to: ", self.frequency)

    def set_transmission_power(self, tx_power):
        self.lora.setOutputPower(tx_power)
        self.tx_power = tx_power
        if self.debug:
            print("Output Power Changed to: ", tx_power, "dBm")

    def get_rssi(self):
        return self.lora.getRSSI()

    def get_snr(self):
        return self.lora.getSNR()

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




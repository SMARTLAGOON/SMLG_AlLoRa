import time
import ubinascii
import network

from AlLoRa.Packet import Packet
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print

# Requires PyLora_SX127x_extensions to be installed
# https://github.com/GRCDEV/PyLora_SX127x_extensions
from PyLora_SX127x_extensions.pyLora import pyLora

class SX127x_connector(Connector):

    def __init__(self): #max_timeout = 10
        super().__init__()

        wlan_sta = network.WLAN(network.STA_IF)
        wlan_sta.active(True)
        wlan_mac = wlan_sta.config('mac')
        self.MAC = str(ubinascii.hexlify(wlan_mac).decode()[-8:])
        wlan_sta.active(False)

    def config(self, config_json):
        super().config(config_json)
        self.lora = pyLora(freq = self.frequency,
                            sf=self.sf, 
                            bw=self.bw,
                            cr=self.cr,
                            output_power=self.tx_power,
                            verbose= self.debug)
        self.lora.setblocking(False)
        if self.debug:
            print("Freq : {}, SF : {}, BW : {}, CR : {}, Power : {}".format(self.lora.get_frequency(), self.lora.get_spreading_factor(), self.lora.get_bandwidth(), self.lora.get_coding_rate(), self.lora.get_transmission_power()))

    def get_rssi(self):
        return self.lora.get_rssi()

    def get_snr(self):
        return self.lora.get_snr()

    def transmit(self, wire):
        if self.debug:
            print("SEND_PACKET() || packet: {}".format(wire))
        if len(wire) <= Connector.MAX_LENGTH_MESSAGE:
            try:
                timeout = max(0.5, self.calculate_toa(self.sf, self.bw, self.cr, len(wire))*1.1)  # Seconds
                t0 = time.ticks_ms()
                if self.sf == 12:
                    timeout *= 1.2
                if self.debug:
                    print("Using timeout: ", timeout, "s to send packet")
                self.lora.settimeout(timeout)
                self.lora.setblocking(True)
                self.lora.send(wire)  # .encode()
                self.lora.setblocking(False)
                td = time.ticks_ms() - t0
                if self.debug:
                    print("Time to send: {} ms and timeout: {} ms".format(td, timeout*1000))
                return True
            except Exception as e:
                if self.debug:
                    print("Error sending packet: ", e)
                self.lora.setblocking(False)
                return False
        else:
            if self.debug:
                print("Error: Packet too big")
            return False

    def recv(self, focus_time=12):
        try:
            t0 = time.ticks_ms()
            self.lora.settimeout(focus_time)
            data = self.lora.recv(Connector.MAX_LENGTH_MESSAGE)
            td = time.ticks_ms() - t0
            if data is None:
                # A frame arrived, the modem said its payload CRC failed, and the driver threw
                # it away. Say so: from up here a corrupt-frame storm and a dead link both look
                # like an empty window, and they call for opposite responses. The timeout path
                # below reaches this same return by raising instead, so the two stay distinct.
                self.recv_dropped_corrupt = True
                if self.debug:
                    print("Dropped a frame with a failed payload CRC")
            return data
        except Exception:
            # Was a bare 'except', which also swallowed KeyboardInterrupt: since the node spends
            # almost all its time blocked in this receive, Ctrl-C never landed and the board could
            # not be dropped into the REPL. 'except Exception' still catches the normal
            # LoRaTimeoutError "no packet in the window" case but lets KeyboardInterrupt through.
            if self.debug:
                print("nothing received or error")
            return None

    def set_frequency(self, freq):
        if self.frequency != freq:
            self.lora.set_frequency(freq)
            self.frequency = freq
            if self.debug:
                print("Frequency Changed to: ", self.frequency, self.lora.get_frequency())

    def set_sf(self, sf):
        if self.sf != sf:
            self.lora.sf(sf)
            self.sf = sf
            if self.debug:
                print("SF Changed to: ", self.sf)

    def set_bw(self, bw):
        if self.debug:
            print("BW:", bw)
        if self.bw != bw:
            try:
                self.lora.set_bandwidth(bw)
                self.bw = bw
                if self.debug:
                    print("BW Changed to: ", self.bw)
            except Exception as e:
                if self.debug:
                    print("Error changing BW: ", e)
        else:
            if self.debug:
                print("BW not changed")

    def set_cr(self, cr):
        if self.cr != cr:
            self.lora.set_coding_rate(cr)
            self.cr = cr
            if self.debug:
                print("CR Changed to: ", self.cr)

    def set_transmission_power(self, desired_power):
        # Set the desired transmission power in dBm
        self.lora.set_transmission_power_dbm(desired_power)
        self.tx_power = desired_power
        if self.debug:
            print("Output Power Changed to: ", desired_power, "dBm")
import ubinascii
import network

from sx1262 import SX1262
from _sx126x import ERR_NONE, ERR_CRC_MISMATCH

from AlLoRa.Packet import Packet
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.time_utils import current_time_ms, ticks_add, ticks_diff, sleep_ms

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

        # From here on the receiver is on air whenever this node is not transmitting. Every
        # method below that leaves the chip somewhere else puts it back.
        self._arm()

    def _arm(self):
        """Leave the receiver listening. Safe to call when it already is.

        Building a receive configuration on this chip costs about 19 ms, so it has to have
        happened already by the time anyone asks for a packet rather than when they ask.
        Doing it inside recv() instead left a node deaf for 44 ms after every transmission,
        measured, and a reply starts arriving within a few ms of a request ending, so a Hub
        on this radio never completed a single poll.

        Arming here is necessary and, on its own, not sufficient: 19 ms is still longer than
        a reply takes to start. Closing the rest of the gap means the driver below batching
        its SPI transfers, which is why an SX1262 still cannot be a Hub.
        """
        try:
            self.lora.startReceive()
        except Exception as e:
            if self.debug:
                print("Arm Error: ", e)

    @staticmethod
    def _driver_cr(cr):
        """AlLoRa counts coding rates 1..4; this driver wants the denominator, 5..8.

        Handed 1 unconverted, `setCodingRate` returns an invalid-coding-rate code and changes
        nothing, and it returns rather than raises, so every coding-rate change on this radio
        used to be accepted and then not happen.
        """
        return cr + 4 if cr <= 4 else cr

    # Each RF setter re-arms: the driver call that applies the change can leave the chip out
    # of receive, and a retune that silently deafened the node would be worse than the gap this
    # class was fixed for. It costs one rebuild per retune, not one per round.
    def set_sf(self, sf):
        if self.sf != sf:
            self.lora.setSpreadingFactor(sf)
            self.sf = sf
            self._arm()
            if self.debug:
                print("SF Changed to: ", self.sf)

    def set_bw(self, bw):
        # Track the live value on self.bw (what get_rf_config / backup_config read), not a
        # 'bw' key in config_parameters that the config loader never reads back (it reads
        # 'bandwidth'): otherwise a committed bandwidth change is lost on reboot.
        if self.bw != bw:
            self.lora.setBandwidth(bw)
            self.bw = bw
            self._arm()
            if self.debug:
                print("BW Set to: ", bw)

    def set_cr(self, cr):
        if self.cr != cr:
            self.lora.setCodingRate(self._driver_cr(cr))
            self.cr = cr
            self._arm()
            if self.debug:
                print("CR Set to: ", cr)

    def set_frequency(self, freq):
        # Without this the base class's no-op ran instead, so a retune to another channel was
        # reported as applied while the radio stayed where it was: both ends then believed they
        # had moved, and only one of them had.
        if self.frequency != freq:
            self.lora.setFrequency(freq)
            self.frequency = freq
            self._arm()
            if self.debug:
                print("Frequency Changed to: ", self.frequency)

    def set_transmission_power(self, tx_power):
        self.lora.setOutputPower(tx_power)
        self.tx_power = tx_power
        self._arm()
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
                self.lora.send(data=wire)
                return True
            except Exception as e:
                if self.debug:
                    print("Send Error: ", e)
                return False
            finally:
                # The driver's transmit ends in standby, so the receiver goes back on air here
                # and not one call later. On the failure path too: a node that could not send is
                # still expected to hear whatever arrives next.
                self._arm()
        else:
            if self.debug:
                print("Error: Packet too big")
            return False

    def recv(self, focus_time=12):
        if not self.lora:
            return None
        try:
            # The receiver is already listening, so a frame that arrived before this call is
            # still in the buffer with DIO1 raised and is taken on the first pass. Nothing here
            # returns the chip to standby: tearing the receiver down and rebuilding it around
            # every call is the whole defect this replaces.
            deadline = ticks_add(current_time_ms(), int(focus_time * 1000))
            while not self.lora.irq.value():
                if ticks_diff(deadline, current_time_ms()) <= 0:
                    return None
                sleep_ms(1)   # the same idle the driver uses in its own wait loops

            # Reads the frame and puts the receiver straight back on air. The driver's public
            # recv() would route to the blocking path, which is the one that starts by going to
            # standby; this is the half of it that does not.
            data, state = self.lora._readData(0)

            if state == ERR_NONE:
                return data

            if state == ERR_CRC_MISMATCH:
                # The frame arrived, the modem said its payload CRC failed, and it was dropped.
                # Say so. From above, a corrupt-frame storm and a dead link both look like an
                # empty window and they call for opposite responses, so Connector.exchange
                # reads this flag to report a corrupt frame instead of silence, and the node
                # counts CorruptedPackets from that label. Without it every damaged frame on
                # this radio is filed as a retransmission and the count is structurally zero.
                self.recv_dropped_corrupt = True
                if self.debug:
                    print("Dropped a frame with a failed payload CRC")
                return None

            if self.debug:
                print("Receive Error: State ", state)
            return None

        except Exception:
            # 'except Exception' rather than a bare except, so Ctrl-C still reaches the REPL
            # from a node that spends almost all its time inside this window.
            if self.debug:
                print("Receive Error")
            return None




import os
import gc
from PyLora_SX127x_extensions.constants import *
import time

# Attempt to get machine type
try:
    machine = os.uname().machine
except Exception:
    machine = os.name

# Enable garbage collection
gc.enable()

def bw_converter(bw):
    if bw == 125:
        bw = BW.BW125
    elif bw == 250:
        bw = BW.BW250
    elif bw == 500:
        bw = BW.BW500
    elif bw == 62.5:
        bw = BW.BW62_5
    elif bw == 41.7:
        bw = BW.BW41_7
    elif bw == 31.25:
        bw = BW.BW31_25
    elif bw == 20.8:
        bw = BW.BW20_8
    elif bw == 15.6:
        bw = BW.BW15_6
    elif bw == 10.4:
        bw = BW.BW10_4
    return bw

def cr_converter(cr):
    if cr == 1:
        cr = CODING_RATE.CR4_5
    elif cr == 2:
        cr = CODING_RATE.CR4_6
    elif cr == 3:
        cr = CODING_RATE.CR4_7
    elif cr == 4:
        cr = CODING_RATE.CR4_8
    return cr

# Bandwidth in Hz, indexed by the MODEM_CONFIG_1 bandwidth field (see the BW class).
BW_HZ = (7800, 10400, 15600, 20800, 31250, 41700, 62500, 125000, 250000, 500000)

# The SX1276 requires LowDataRateOptimize once a symbol lasts longer than 16 ms:
# past that, crystal drift accumulates over the symbol and the receiver loses lock.
# At BW125 that means SF11 and SF12, at BW250 it means SF12. The threshold is on
# symbol time, not on the spreading factor, so bandwidth has to be part of the test.
LDRO_SYMBOL_TIME_S = 0.016


class pyLora:
    IS_RPi = machine.startswith('armv')
    IS_ESP8266 = machine.startswith('ESP8266')
    IS_ESP32 = machine.startswith('ESP32') and not machine.startswith('Generic ESP32S3')
    IS_LORA32 = machine.startswith('LILYGO')
    IS_ESP32S3 = machine.startswith('Generic ESP32S3')  # Corrected check for ESP32S3

    __SX127X_LIB = None

    timeout_socket = None
    blocked_socket = None

    # While true, LDRO is recomputed from SF and BW on every change. set_low_data_rate_optim()
    # clears it so a deliberate override is not silently undone by the next set_spreading_factor;
    # auto_low_data_rate_optim() hands control back.
    _ldro_auto = True

    def __init__(self, verbose=False, do_calibration=False, calibration_freq=868, 
                    sf=7, cr=1, freq=868, bw=125, pa_select=1, 
                    max_power=7, output_power=14, preamble=8):
        auto_board_selection = None

        if self.IS_RPi:
            from PyLora_SX127x_extensions.board_config_rpi import BOARD_RPI
            auto_board_selection = BOARD_RPI

        elif self.IS_ESP32 or self.IS_LORA32:
            from PyLora_SX127x_extensions.board_config_esp32 import BOARD_ESP32
            auto_board_selection = BOARD_ESP32

        elif self.IS_ESP32S3:
            # Import and use the BOARD_ESP32S3 class
            from PyLora_SX127x_extensions.board_config_esp32s3 import BOARD_ESP32S3
            auto_board_selection = BOARD_ESP32S3

        bw = bw_converter(bw)
        cr = cr_converter(cr)


        # Collect garbage and check the amount of memory allocated and free again
        gc.collect()
        from PyLora_SX127x_extensions.LoRa import LoRa
        self.__SX127X_LIB = LoRa(Board_specification=auto_board_selection,
                                 verbose=verbose,
                                 do_calibration=do_calibration,
                                 calibration_freq=calibration_freq,
                                 cr=cr,
                                 sf=sf,
                                 freq=freq,
                                 rx_crc=True, 
                                 signal_bandwidth=bw,
                                 pa_select=pa_select,
                                 max_power=max_power,
                                 output_power=output_power,
                                 preamble=preamble)
        self._apply_ldro()

    def _apply_ldro(self):
        """ Match LowDataRateOptimize to the spreading factor and bandwidth now in use.

            The modem is read back rather than trusting whatever a caller passed in, so
            this stays correct whichever setter got us here and whether the caller used
            kHz or a BW constant. Does nothing while an override is in force.
        """
        if not self._ldro_auto:
            return
        sf = self.get_spreading_factor()
        bw = self.get_bandwidth()
        if not 0 <= bw < len(BW_HZ):
            return      # bandwidth we cannot price: leave the register as it is
        symbol_time = (2 ** sf) / BW_HZ[bw]
        ldro = 1 if symbol_time > LDRO_SYMBOL_TIME_S else 0

        # Modem config only latches in sleep or standby, and a node parks in continuous
        # receive between transfers, so this can land mid-RX. Same guard change_bw uses.
        mode = self.__SX127X_LIB.get_mode()
        if mode != MODE.STDBY:
            self.__SX127X_LIB.set_mode(MODE.STDBY)
        self.__SX127X_LIB.set_low_data_rate_optim(ldro)
        if mode != MODE.STDBY:
            self.__SX127X_LIB.set_mode(mode)

    def send(self, content):
        self.__SX127X_LIB.set_mode(MODE.SLEEP)
        self.__SX127X_LIB.set_dio_mapping([1, 0, 0, 0])  # DIO0 = 1 is for TXDone, transmitting mode basically
        self.__SX127X_LIB.set_mode(MODE.STDBY)
        self.__SX127X_LIB.write_payload(list(content))  # I send my payload to LORA SX1276 interface
        self.__SX127X_LIB.set_mode(MODE.TX)  # I enter on TX Mode

        # Wait for TXDone
        while not self.__SX127X_LIB.get_irq_flags()['tx_done']:
            pass
        self.__SX127X_LIB.set_dio0_status(timeout_value=self.timeout_socket, socket_blocked=self.blocked_socket)   #self.timeout_socket

    def recv(self, size=230):
        """ Util Method for recv
            It will turn automatically the device on receive mode
        """
        self.__SX127X_LIB.set_mode(MODE.SLEEP)
        self.__SX127X_LIB.set_dio_mapping([0, 0, 0, 0])
        self.__SX127X_LIB.set_mode(MODE.RXCONT)
        self.__SX127X_LIB.set_dio0_status(timeout_value=self.timeout_socket, socket_blocked=self.blocked_socket)
        return bytes(self.__SX127X_LIB.payload)

    def settimeout(self, value):
        """ set timeout for operations
            After we determine if we want to send or receive, we need to specify a timeout
        """
        self.timeout_socket = value


    def setblocking(self, value):
        self.blocked_socket = value

    def get_rssi(self):
        return self.__SX127X_LIB.get_pkt_rssi_value()

    def get_snr(self):
        return self.__SX127X_LIB.get_pkt_snr_value()

    # Added method to get spreading factor
    def get_spreading_factor(self):
        config = self.__SX127X_LIB.get_modem_config_2()
        return config['spreading_factor']

    # Added method to get bandwidth
    def get_bandwidth(self):
        config = self.__SX127X_LIB.get_modem_config_1()
        return config['bw']

    # Added method to get coding rate
    def get_coding_rate(self):
        config = self.__SX127X_LIB.get_modem_config_1()
        return config['coding_rate']

    # Added method to get frequency
    def get_frequency(self):
        return self.__SX127X_LIB.get_freq()

    # Added method to get transmission power
    def get_transmission_power(self, convert_dBm=False):
        pa_config = self.__SX127X_LIB.get_pa_config(convert_dBm=convert_dBm)
        return pa_config['output_power']

    # Added method to get max_power and output_power
    def get_pa_config(self, convert_dBm=False):
        pa_config = self.__SX127X_LIB.get_pa_config(convert_dBm=convert_dBm)
        return {
            'max_power': pa_config['max_power'],
            'output_power': pa_config['output_power']
        }

    # Added method to get preamble length
    def get_preamble(self):
        return self.__SX127X_LIB.get_preamble()

    def sf(self, sf):
        self.__SX127X_LIB.set_spreading_factor(sf)
        self._apply_ldro()

    # Added method to set spreading factor
    def set_spreading_factor(self, sf):
        self.__SX127X_LIB.set_spreading_factor(sf)
        self._apply_ldro()

    # Added method to set bandwidth
    def set_bandwidth(self, bw):
        bw = bw_converter(bw)
        print("BW:", bw)
        #self.__SX127X_LIB.change_bw(bw)
        self.__SX127X_LIB.change_bw(bw)
        self._apply_ldro()

    # Added method to get low data rate optimization
    def get_low_data_rate_optim(self):
        return self.__SX127X_LIB.get_low_data_rate_optim()

    # Added method to force low data rate optimization, overriding the automatic choice.
    # The override is sticky: it survives later SF and BW changes, so an A/B measurement
    # cannot be silently undone by setting the spreading factor afterwards. Call
    # auto_low_data_rate_optim() to go back to following the modem.
    def set_low_data_rate_optim(self, low_data_rate_optim):
        self._ldro_auto = False
        self.__SX127X_LIB.set_low_data_rate_optim(1 if low_data_rate_optim else 0)

    # Added method to hand LDRO back to the automatic symbol-time rule, and apply it now.
    def auto_low_data_rate_optim(self):
        self._ldro_auto = True
        self._apply_ldro()

    # Added method to set coding rate
    def set_coding_rate(self, cr):
        self.__SX127X_LIB.set_coding_rate(cr)

    # Added method to set frequency
    def set_frequency(self, freq):
        self.__SX127X_LIB.change_frequency(freq)

    # Added method to set transmission power
    def set_transmission_power(self, pa_select, max_power, output_power):
        return self.__SX127X_LIB.set_pa_config(pa_select=pa_select, max_power=max_power, output_power=output_power)

    # Added method to set preamble length
    def set_preamble(self, preamble):
        self.__SX127X_LIB.set_preamble(preamble)

    def set_pa_config(self, pa_select, max_power, output_power):
        if not (0 <= max_power <= 7):
            raise ValueError("max_power must be between 0 and 7")
        if not (0 <= output_power <= 15):
            raise ValueError("output_power must be between 0 and 15")
        self.__SX127X_LIB.set_pa_config(pa_select=pa_select, max_power=max_power, output_power=output_power)

    def set_transmission_power_dbm(self, desired_power):
        """ Set the desired transmission power in dBm
        :param desired_power: Desired output power in dBm
        """
        pa_select = 1  # Use PA_BOOST
        max_power = 7  # Max power level to get P_max = 15 dBm

        # Calculate output_power based on desired_power
        # output_power = 17 - desired_power
        # if output_power < 0 or output_power > 15:
        #     raise ValueError("Desired power is out of range. It must be between 2 and 17 dBm for PA_BOOST.")
        
        # Max power 15 dBm, output power 0-15 dBm
        output_power = min(15, desired_power)
        output_power = max(0, output_power)

        # Set the PA configuration
        self.set_pa_config(pa_select=pa_select, max_power=max_power, output_power=output_power)


# Example usage:
# lora = pyLora(verbose=True, sf=7, bw=BW.BW125, cr=CODING_RATE.CR4_5, freq=868.0, pa_select=1, max_power=7, output_power=15, preamble=8)
# print("RSSI:", lora.get_rssi())
# print("SNR:", lora.get_snr())
# print("SF:", lora.get_spreading_factor())
# print("BW:", lora.get_bandwidth())
# print("CR:", lora.get_coding_rate())
# print("Frequency:", lora.get_frequency())
# print("Transmission Power:", lora.get_transmission_power())
# print("Preamble:", lora.get_preamble())
# print("PA Config:", lora.get_pa_config())
# lora.set_pa_config(pa_select=1, max_power=7, output_power=15)
# print("Updated PA Config:", lora.get_pa_config())
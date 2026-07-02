""" Defines the SX127x class and a few utility functions. """
import sys
from PyLora_SX127x_extensions.constants import *

################################################## Some utility functions ##############################################

def set_bit(value, index, new_bit):
    mask = 1 << index
    value &= ~mask
    if new_bit:
        value |= mask
    return value

def getter(register_address):
    """ The getter decorator reads the register content and calls the decorated function to do
        post-processing.
    :param register_address: Register address
    :return: Register value
    :rtype: int
    """

    def decorator(func):
        def wrapper(self):
            v = self.spi.transfer(address=register_address)
            return func(self, v)

        return wrapper

    return decorator


def setter(register_address):
    """ The setter decorator calls the decorated function for pre-processing and
        then writes the result to the register
    :param register_address: Register address
    :return: New register value
    :rtype: int
    """

    def decorator(func):
        def wrapper(self, val):
            v = self.spi.transfer(address=register_address | 0x80, value=func(self, val))
            return v

        return wrapper

    return decorator


############################################### Definition of the LoRa class ###########################################

class LoRa:
    selected_chip = None
    spi = None  # init and get the board's SPI
    mode = None  # the mode is backed up here
    verbose = True
    dio_mapping = [None] * 4  # store the dio mapping here

    def __init__(self,
                 Board_specification=None,
                 verbose=True,
                 do_calibration=True,
                 calibration_freq=868,
                 sf=7,
                 cr=CODING_RATE.CR4_5,
                 freq=868,
                 sync_word=0x34,
                 rx_crc=False,
                 signal_bandwidth=BW.BW125,
                 pa_select=1,
                 max_power=7,
                 output_power=14, 
                 preamble=8):

        if Board_specification is None:
            raise NotImplemented("A Board specification is needed for SX127X to work.")

        # Initialize corresponding Board
        self.selected_chip = Board_specification()

        # Create real SPI object
        self.selected_chip.init_spi()

        self.selected_chip.set_irq_callbacks(cb_dio0=self._dio0)
        # Get working class with util methods of SPI
        self.spi = self.selected_chip.get_spi()
        self.set_implicit_header_mode(False)
        self.set_invert_iq(0)
        self.payload = ""
        self.freq = freq
        self.verbose = verbose
        self.set_bw(signal_bandwidth)

        # set mode to sleep and read all registers
        self.set_mode(MODE.SLEEP)

        # more setup work:
        if do_calibration:
            self.rx_chain_calibration(calibration_freq)
        # the FSK registers are set up exactly as modtronix do it:
        lookup_fsk = [
            # [REG.FSK.LNA            , 0x23],
            # [REG.FSK.RX_CONFIG      , 0x1E],
            # [REG.FSK.RSSI_CONFIG    , 0xD2],
            # [REG.FSK.PREAMBLE_DETECT, 0xAA],
            # [REG.FSK.OSC            , 0x07],
            # [REG.FSK.SYNC_CONFIG    , 0x12],
            # [REG.FSK.SYNC_VALUE_1   , 0xC1],
            # [REG.FSK.SYNC_VALUE_2   , 0x94],
            # [REG.FSK.SYNC_VALUE_3   , 0xC1],
            # [REG.FSK.PACKET_CONFIG_1, 0xD8],
            # [REG.FSK.FIFO_THRESH    , 0x8F],
            # [REG.FSK.IMAGE_CAL      , 0x02],
            # [REG.FSK.DIO_MAPPING_1  , 0x00],
            # [REG.FSK.DIO_MAPPING_2  , 0x30]
        ]
        self.set_mode(MODE.FSK_STDBY)
        for register_address, value in lookup_fsk:
            self.set_register(register_address, value)

        self.set_mode(MODE.SLEEP)
        self.set_coding_rate(cr)
        self.set_freq(freq)
        self.set_spreading_factor(sf)
        self.set_sync_word(sync_word)
        self.set_rx_crc(rx_crc)

        # set the dio_ mapping by calling the two get_dio_mapping_* functions
        self.get_dio_mapping_1()

        self.set_mode(MODE.STDBY)
        self.set_preamble(preamble)
        self.set_pa_config(pa_select=pa_select, output_power=output_power, max_power=max_power)

    # Overridable functions:

    def on_rx_done(self):
        self.clear_irq_flags(RxDone=1)  # clear rxdone IRQ flag
        self.payload = self.read_payload(nocheck=True)
        self.reset_ptr_rx()  # we clear pointer
        self.set_mode(MODE.RXCONT)

    def on_tx_done(self):
        self.set_mode(MODE.STDBY)
        #print('fifo ', self.get_fifo_rx_current_addr())
        self.clear_irq_flags(TxDone=1)  # clear txdone IRQ flag

    def on_cad_done(self):
        pass

    def on_rx_timeout(self):
        pass

    def on_valid_header(self):
        pass

    def on_payload_crc_error(self):
        pass

    def on_fhss_change_channel(self):
        pass

        # Internal callbacks for DIO0

    def _dio0(self, channel):
        # DIO0 00: RxDone
        # DIO0 01: TxDone
        # DIO0 10: CadDone
        if self.dio_mapping[0] == 0:
            self.on_rx_done()
        elif self.dio_mapping[0] == 1:
            self.on_tx_done()
        elif self.dio_mapping[0] == 2:
            self.on_cad_done()
        else:
            raise RuntimeError("unknown dio0mapping!")

        # All the set/get/read/write functions

    def set_dio0_status(self, timeout_value=None, socket_blocked=None):
        self.selected_chip.add_event_dio0(value=timeout_value, blocked=socket_blocked)

    def get_mode(self):
        """ Get the mode
        :return:    New mode
        """
        self.mode = self.spi.transfer(address=REG.LORA.OP_MODE)
        return self.mode

    def set_mode(self, mode):
        """ Set the mode
        :param mode: Set the mode. Use constants.MODE class
        :return:    New mode
        """
        # the mode is backed up in self.mode
        if mode == self.mode:
            return mode
        if self.verbose:
            sys.stderr.write("Mode <- %s\n" % MODE.lookup[mode])
        self.mode = mode
        v = self.spi.transfer(address=REG.LORA.OP_MODE | 0x80, value=mode)
        return v


    def write_payload(self, payload):
        """ Get FIFO ready for TX: Set FifoAddrPtr to FifoTxBaseAddr. The transceiver is put into STDBY mode.
        :param payload: Payload to write (list)
        :return:    Written payload
        """
        payload_size = len(payload)
        self.set_mode(MODE.STDBY)
        self.set_payload_length(payload_size)
        base_addr = self.get_fifo_tx_base_addr()
        self.set_fifo_addr_ptr(base_addr)
        for i in range(payload_size):
            x = self.spi.transfer(address=REG.LORA.FIFO | 0x80, value=payload[i])

        return payload

    def reset_ptr_rx(self):
        """ Get FIFO ready for RX: Set FifoAddrPtr to FifoRxBaseAddr. The transceiver is put into STDBY mode. """
        self.set_mode(MODE.STDBY)
        base_addr = self.get_fifo_rx_base_addr()
        self.set_fifo_addr_ptr(base_addr)

    def rx_is_good(self):
        """ Check the IRQ flags for RX errors
        :return: True if no errors
        :rtype: bool
        """
        flags = self.get_irq_flags()
        return not any([flags[s] for s in ['valid_header', 'crc_error', 'rx_done', 'rx_timeout']])

    def read_payload(self, nocheck=False):
        """ Read the payload from FIFO
        :param nocheck: If True then check rx_is_good()
        :return: Payload
        :rtype: list[int]
        """
        if not nocheck and not self.rx_is_good():
            return None
        rx_nb_bytes = self.get_rx_nb_bytes()
        fifo_rx_current_addr = self.get_fifo_rx_current_addr()
        self.set_fifo_addr_ptr(fifo_rx_current_addr)
        payload = bytearray()
        for i in range(rx_nb_bytes):
            payload.append(self.spi.transfer(address=REG.LORA.FIFO))
        return payload

    def get_freq(self):
        """ Get the frequency (MHz)
        :return:    Frequency in MHz
        :rtype:     float
        """
        msb = self.spi.transfer(address=REG.LORA.FR_MSB)
        mid = self.spi.transfer(address=REG.LORA.FR_MID)
        lsb = self.spi.transfer(address=REG.LORA.FR_LSB)

        f = lsb + 256 * (mid + 256 * msb)
        return f / 16384.

    def set_freq(self, f):
        """ Set the frequency (MHz)
        :param f: Frequency in MHz
        "type f: float
        :return: New register settings (3 bytes [msb, mid, lsb])
        :rtype: list[int]
        """
        assert self.mode == MODE.SLEEP or self.mode == MODE.STDBY or self.mode == MODE.FSK_STDBY
        i = int(f * 16384.)  # choose floor
        msb = i // 65536
        i -= msb * 65536
        mid = i // 256
        i -= mid * 256
        lsb = i

        msb = self.spi.transfer(address=REG.LORA.FR_MSB | 0x80, value=msb)
        mid = self.spi.transfer(address=REG.LORA.FR_MID | 0x80, value=mid)
        lsb = self.spi.transfer(address=REG.LORA.FR_LSB | 0x80, value=lsb)

        return [msb, mid, lsb]

    def get_pa_config(self, convert_dBm=False):
        v = self.spi.transfer(address=REG.LORA.PA_CONFIG)
        pa_select = v >> 7
        max_power = v >> 4 & 0b111
        output_power = v & 0b1111
        if convert_dBm:
            max_power = max_power * .6 + 10.8
            if pa_select == 0:
                output_power = max_power - (15 - output_power)
            else:
                output_power = 17 - (15 - output_power)
        return dict(
            pa_select=pa_select,
            max_power=max_power,
            output_power=output_power
        )

    def set_pa_config(self, pa_select=None, max_power=None, output_power=None):
        """ Configure the PA
        :param pa_select: Selects PA output pin, 0->RFO, 1->PA_BOOST
        :param max_power: Select max output power Pmax=10.8+0.6*MaxPower
        :param output_power: Output power Pout=Pmax-(15-OutputPower) if PaSelect = 0,
                Pout=17-(15-OutputPower) if PaSelect = 1 (PA_BOOST pin)
        :return: new register value
        """
        print("Setting PA config")
        print("pa_select: ", pa_select)
        print("max_power: ", max_power)
        print("output_power: ", output_power)
        loc = locals()
        current = self.get_pa_config()

        if pa_select is not None:
            current['pa_select'] = pa_select

        if max_power is not None:
            current['max_power'] = max_power

        if output_power is not None:
            current['output_power'] = output_power

        val = (current['pa_select'] << 7) | (current['max_power'] << 4) | (current['output_power'])
        print("Setting PA config to: ", val)
        v = self.spi.transfer(address=REG.LORA.PA_CONFIG | 0x80, value=val)
        return v

    @getter(REG.LORA.PA_RAMP)
    def get_pa_ramp(self, val):
        return val & 0b1111

    @setter(REG.LORA.PA_RAMP)
    def set_pa_ramp(self, val):
        return val & 0b1111

    def get_ocp(self, convert_mA=False):

        v = self.spi.transfer(address=REG.LORA.OCP)

        ocp_on = v >> 5 & 0x01
        ocp_trim = v & 0b11111
        if convert_mA:
            if ocp_trim <= 15:
                ocp_trim = 45. + 5. * ocp_trim
            elif ocp_trim <= 27:
                ocp_trim = -30. + 10. * ocp_trim
            else:
                assert ocp_trim <= 27
        return dict(
            ocp_on=ocp_on,
            ocp_trim=ocp_trim
        )

    def set_ocp_trim(self, I_mA):
        assert (I_mA >= 45 and I_mA <= 240)

        ocp_on = self.spi.transfer(address=REG.LORA.OCP) >> 5 & 0x01

        if I_mA <= 120:
            v = int(round((I_mA - 45.) / 5.))
        else:
            v = int(round((I_mA + 30.) / 10.))
        v = set_bit(v, 5, ocp_on)

        v = self.spi.transfer(address=REG.LORA.OCP | 0x80, value=v)

        return v

    def get_modem_config_1(self):
        val = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1)
        return dict(
            bw=val >> 4 & 0x0F,
            coding_rate=val >> 1 & 0x07,
            implicit_header_mode=val & 0x01
        )

    def get_modem_config_2(self, include_symb_timout_lsb=False):
        val = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2)
        d = dict(
            spreading_factor=val >> 4 & 0x0F,
            tx_cont_mode=val >> 3 & 0x01,
            rx_crc=val >> 2 & 0x01,
        )
        if include_symb_timout_lsb:
            d['symb_timout_lsb'] = val & 0x03
        return d

    def get_modem_config_3(self):
        val = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_3)
        return dict(
            low_data_rate_optim=val >> 3 & 0x01,
            agc_auto_on=val >> 2 & 0x01
        )

    def get_lna(self):

        v = self.spi.transfer(address=REG.LORA.LNA)

        return dict(
            lna_gain=v >> 5,
            lna_boost_lf=v >> 3 & 0b11,
            lna_boost_hf=v & 0b11
        )

    def set_lna(self, lna_gain=None, lna_boost_lf=None, lna_boost_hf=None):
        assert lna_boost_hf is None or lna_boost_hf == 0b00 or lna_boost_hf == 0b11
        self.set_mode(MODE.STDBY)
        if lna_gain is not None:
            # Apparently agc_auto_on must be 0 in order to set lna_gain
            self.set_agc_auto_on(lna_gain == GAIN.NOT_USED)
        loc = locals()
        current = self.get_lna()
        loc = {s: current[s] if loc[s] is None else loc[s] for s in loc}
        val = (loc['lna_gain'] << 5) | (loc['lna_boost_lf'] << 3) | (loc['lna_boost_hf'])

        retval = self.spi.transfer(address=REG.LORA.LNA | 0x80, value=val)

        if lna_gain is not None:
            # agc_auto_on must track lna_gain: GAIN=NOT_USED -> agc_auto=ON, otherwise =OFF
            self.set_agc_auto_on(lna_gain == GAIN.NOT_USED)
        return retval

    def set_lna_gain(self, lna_gain):
        self.set_lna(lna_gain=lna_gain)

    def get_fifo_addr_ptr(self):

        v = self.spi.transfer(address=REG.LORA.FIFO_ADDR_PTR)

        return v

    def set_fifo_addr_ptr(self, ptr):

        v = self.spi.transfer(address=REG.LORA.FIFO_ADDR_PTR | 0x80, value=ptr)

        return v

    def get_fifo_tx_base_addr(self):

        v = self.spi.transfer(address=REG.LORA.FIFO_TX_BASE_ADDR)

        return v

    def set_fifo_tx_base_addr(self, ptr):

        v = self.spi.transfer(address=REG.LORA.FIFO_TX_BASE_ADDR | 0x80, value=ptr)

        return v

    def get_fifo_rx_base_addr(self):

        v = self.spi.transfer(address=REG.LORA.FIFO_RX_BASE_ADDR)

        return v

    def set_fifo_rx_base_addr(self, ptr):

        v = self.spi.transfer(address=REG.LORA.FIFO_RX_BASE_ADDR | 0x80, value=ptr)

        return v

    def get_fifo_rx_current_addr(self):

        v = self.spi.transfer(address=REG.LORA.FIFO_RX_CURR_ADDR)

        return v

    def get_fifo_rx_byte_addr(self):

        v = self.spi.transfer(address=REG.LORA.FIFO_RX_BYTE_ADDR)

        return v

    def get_irq_flags_mask(self):

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS_MASK)

        return dict(
            rx_timeout=v >> 7 & 0x01,
            rx_done=v >> 6 & 0x01,
            crc_error=v >> 5 & 0x01,
            valid_header=v >> 4 & 0x01,
            tx_done=v >> 3 & 0x01,
            cad_done=v >> 2 & 0x01,
            fhss_change_ch=v >> 1 & 0x01,
            cad_detected=v >> 0 & 0x01,
        )

    def set_irq_flags_mask(self,
                           rx_timeout=None, rx_done=None, crc_error=None, valid_header=None, tx_done=None,
                           cad_done=None, fhss_change_ch=None, cad_detected=None):
        loc = locals()

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS_MASK)

        for i, s in enumerate(['cad_detected', 'fhss_change_ch', 'cad_done', 'tx_done', 'valid_header',
                               'crc_error', 'rx_done', 'rx_timeout']):
            this_bit = locals()[s]
            if this_bit is not None:
                v = set_bit(v, i, this_bit)

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS_MASK | 0x80, value=v)

        return v

    def get_irq_flags(self):

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS)

        return dict(
            rx_timeout=v >> 7 & 0x01,
            rx_done=v >> 6 & 0x01,
            crc_error=v >> 5 & 0x01,
            valid_header=v >> 4 & 0x01,
            tx_done=v >> 3 & 0x01,
            cad_done=v >> 2 & 0x01,
            fhss_change_ch=v >> 1 & 0x01,
            cad_detected=v >> 0 & 0x01,
        )

    def set_irq_flags(self,
                      rx_timeout=None, rx_done=None, crc_error=None, valid_header=None, tx_done=None,
                      cad_done=None, fhss_change_ch=None, cad_detected=None):

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS)

        for i, s in enumerate(['cad_detected', 'fhss_change_ch', 'cad_done', 'tx_done', 'valid_header',
                               'crc_error', 'rx_done', 'rx_timeout']):
            this_bit = locals()[s]
            if this_bit is not None:
                v = set_bit(v, i, this_bit)

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS | 0x80, value=v)

        return v

    def clear_irq_flags(self,
                        RxTimeout=None, RxDone=None, PayloadCrcError=None,
                        ValidHeader=None, TxDone=None, CadDone=None,
                        FhssChangeChannel=None, CadDetected=None):
        v = 0

        d = dict(CadDetected=CadDetected,
                 FhssChangeChannel=FhssChangeChannel,
                 CadDone=CadDone,
                 TxDone=TxDone,
                 ValidHeader=ValidHeader,
                 PayloadCrcError=PayloadCrcError,
                 RxDone=RxDone,
                 RxTimeout=RxTimeout)

        for i, s in enumerate(['CadDetected', 'FhssChangeChannel', 'CadDone',
                               'TxDone', 'ValidHeader', 'PayloadCrcError',
                               'RxDone', 'RxTimeout']):
            this_bit = d[s]
            if this_bit is not None:
                v = set_bit(v, eval('MASK.IRQ_FLAGS.' + s), this_bit)

        v = self.spi.transfer(address=REG.LORA.IRQ_FLAGS | 0x80, value=v)

        return v

    def get_rx_nb_bytes(self):

        v = self.spi.transfer(address=REG.LORA.RX_NB_BYTES)

        return v

    def get_pkt_snr_value(self):

        v = self.spi.transfer(address=REG.LORA.PKT_SNR_VALUE)

        return (float(v - 256) if v > 127 else float(v)) / 4.

    def get_pkt_rssi_value(self):

        v = self.spi.transfer(address=REG.LORA.PKT_RSSI_VALUE)

        return v - (164 if self.selected_chip.low_band else 157)  # See datasheet 5.5.5. p. 87

    def get_rssi_value(self):

        v = self.spi.transfer(address=REG.LORA.RSSI_VALUE)

        return v - (164 if self.selected_chip.low_band else 157)  # See datasheet 5.5.5. p. 87

    def get_hop_channel(self):

        v = self.spi.transfer(address=REG.LORA.HOP_CHANNEL)

        return dict(
            pll_timeout=v >> 7,
            crc_on_payload=v >> 6 & 0x01,
            fhss_present_channel=v >> 5 & 0b111111
        )

    def set_bw(self, bw):
        """ Set the bandwidth 0=7.8kHz ... 9=500kHz
        :param bw: A number 0,2,3,...,9
        :return:
        """
        config = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1)
        s = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1 | 0x80, value=(config & 0x0f) | (bw << 4))

    def set_coding_rate(self, coding_rate):
        """ Set the coding rate 4/5, 4/6, 4/7, 4/8
        :param coding_rate: A number 1,2,3,4
        :return: New register value
        """
        config = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1)
        self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1 | 0x80, value=(config & 0xf1) | (coding_rate << 1))

    def set_implicit_header_mode(self, implicit_header_mode):
        config_modem = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1)
        config = config_modem | 0x01 if implicit_header_mode else config_modem & 0xfe
        v = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_1 | 0x80, value=config)

    def set_spreading_factor(self, spreading_factor):
        spreading_factor = min(max(spreading_factor, 6), 12)

        config = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2)
        self.spi.transfer(address=REG.LORA.DETECT_OPTIMIZE | 0x80, value=0xc5 if spreading_factor == 6 else 0xc3)
        self.spi.transfer(address=REG.LORA.DETECTION_THRESH | 0x80, value=0x0c if spreading_factor == 6 else 0x0a)

        self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2 | 0x80,
                          value=(config & 0x0f) | ((spreading_factor << 4) & 0xf0))

    def set_rx_crc(self, rx_crc):
        config_modem = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2)
        config = config_modem | 0x04 if rx_crc else config_modem & 0xfb
        self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2 | 0x80, value=config)

    def get_modem_config_3(self):

        val = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_3)

        return dict(
            low_data_rate_optim=val >> 3 & 0x01,
            agc_auto_on=val >> 2 & 0x01
        )

    def set_modem_config_3(self, low_data_rate_optim=None, agc_auto_on=None):
        loc = locals()
        current = self.get_modem_config_3()

        if low_data_rate_optim is not None:
            current['low_data_rate_optim'] = low_data_rate_optim

        if agc_auto_on is not None:
            current['agc_auto_on'] = agc_auto_on

        val = (current['low_data_rate_optim'] << 3) | (current['agc_auto_on'] << 2)

        v = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_3 | 0x80, value=val)

        return v

    @setter(REG.LORA.INVERT_IQ)
    def set_invert_iq(self, invert):
        """ Invert the LoRa I and Q signals
        :param invert: 0: normal mode, 1: I and Q inverted
        :return: New value of register
        """
        return 0x27 | (invert & 0x01) << 6

    @getter(REG.LORA.INVERT_IQ)
    def get_invert_iq(self, val):
        """ Get the invert the I and Q setting
        :return: 0: normal mode, 1: I and Q inverted
        """
        return (val >> 6) & 0x01

    def get_agc_auto_on(self):
        return self.get_modem_config_3()['agc_auto_on']

    def set_agc_auto_on(self, agc_auto_on):
        self.set_modem_config_3(agc_auto_on=agc_auto_on)

    def get_low_data_rate_optim(self):
        return self.set_modem_config_3()['low_data_rate_optim']

    def set_low_data_rate_optim(self, low_data_rate_optim):
        self.set_modem_config_3(low_data_rate_optim=low_data_rate_optim)

    def get_symb_timeout(self):
        SYMB_TIMEOUT_MSB = REG.LORA.MODEM_CONFIG_2

        msb, lsb = self.spi.transfer(address=SYMB_TIMEOUT_MSB)[:]  # the MSB bits are stored in REG.LORA.MODEM_CONFIG_2

        msb = msb & 0b11
        return lsb + 256 * msb

    def set_symb_timeout(self, timeout):

        bkup_reg_modem_config_2 = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2)

        msb = timeout >> 8 & 0b11  # bits 8-9
        lsb = timeout - 256 * msb  # bits 0-7
        reg_modem_config_2 = bkup_reg_modem_config_2 & 0xFC | msb  # bits 2-7 of bkup_reg_modem_config_2 ORed with the two msb bits

        old_msb = self.spi.transfer(address=REG.LORA.MODEM_CONFIG_2 | 0x80, value=reg_modem_config_2) & 0x03

        old_lsb = self.spi.transfer(address=REG.LORA.SYMB_TIMEOUT_LSB | 0x80, value=lsb)

        return old_lsb + 256 * old_msb

    def get_preamble(self):

        msb = self.spi.transfer(address=REG.LORA.PREAMBLE_MSB)
        lsb = self.spi.transfer(address=REG.LORA.PREAMBLE_LSB)

        return lsb + 256 * msb

    def set_preamble(self, preamble):
        msb = preamble >> 8 & 0xff
        lsb = preamble >> 0 & 0xff

        old_msb = self.spi.transfer(address=REG.LORA.PREAMBLE_MSB | 0x80, value=msb)
        old_lsb = self.spi.transfer(address=REG.LORA.PREAMBLE_LSB | 0x80, value=lsb)

        return old_lsb + 256 * old_msb

    @getter(REG.LORA.PAYLOAD_LENGTH)
    def get_payload_length(self, val):
        return val

    @setter(REG.LORA.PAYLOAD_LENGTH)
    def set_payload_length(self, payload_length):
        return payload_length

    @getter(REG.LORA.MAX_PAYLOAD_LENGTH)
    def get_max_payload_length(self, val):
        return val

    @setter(REG.LORA.MAX_PAYLOAD_LENGTH)
    def set_max_payload_length(self, max_payload_length):
        return max_payload_length

    @getter(REG.LORA.HOP_PERIOD)
    def get_hop_period(self, val):
        return val

    @setter(REG.LORA.HOP_PERIOD)
    def set_hop_period(self, hop_period):
        return hop_period

    @getter(REG.LORA.DETECT_OPTIMIZE)
    def get_detect_optimize(self, val):
        """ Get LoRa detection optimize setting
        :return: detection optimize setting 0x03: SF7-12, 0x05: SF6
        """
        return val & 0b111

    @setter(REG.LORA.DETECT_OPTIMIZE)
    def set_detect_optimize(self, detect_optimize):
        """ Set LoRa detection optimize
        :param detect_optimize 0x03: SF7-12, 0x05: SF6
        :return: New register value
        """
        assert detect_optimize == 0x03 or detect_optimize == 0x05
        return detect_optimize & 0b111

    @getter(REG.LORA.DETECTION_THRESH)
    def get_detection_threshold(self, val):
        """ Get LoRa detection threshold setting
        :return: detection threshold 0x0A: SF7-12, 0x0C: SF6
        """
        return val

    @setter(REG.LORA.DETECTION_THRESH)
    def set_detection_threshold(self, detect_threshold):
        """ Set LoRa detection optimize
        :param detect_threshold 0x0A: SF7-12, 0x0C: SF6
        :return: New register value
        """
        assert detect_threshold == 0x0A or detect_threshold == 0x0C
        return detect_threshold

    @getter(REG.LORA.SYNC_WORD)
    def get_sync_word(self, sync_word):
        return sync_word

    @setter(REG.LORA.SYNC_WORD)
    def set_sync_word(self, sync_word):
        return sync_word

    @getter(REG.LORA.DIO_MAPPING_1)
    def get_dio_mapping_1(self, mapping):
        """ Get mapping of pins DIO0 to DIO3. Object variable dio_mapping will be set.
        :param mapping: Register value
        :type mapping: int
        :return: Value of the mapping list
        :rtype: list[int]
        """

        self.dio_mapping = [mapping >> 6 & 0x03, mapping >> 4 & 0x03, mapping >> 2 & 0x03, mapping >> 0 & 0x03]
        return self.dio_mapping

    @setter(REG.LORA.DIO_MAPPING_1)
    def set_dio_mapping_1(self, mapping):
        """ Set mapping of pins DIO0  Object variable dio_mapping will be set.
        :param mapping: Register value
        :type mapping: int
        :return: New value of the register
        :rtype: int
        """
        self.dio_mapping = [mapping >> 6 & 0x03, mapping >> 4 & 0x03, mapping >> 2 & 0x03, mapping >> 0 & 0x03]
        return mapping

    def get_dio_mapping(self):
        return self.dio_mapping

    def set_dio_mapping(self, mapping):
        """ Utility function that returns the list of current DIO mappings. Object variable dio_mapping will be set.
        :param mapping: DIO mapping list
        :type mapping: list[int]
        :return: New DIO mapping list
        :rtype: list[int]
        """
        mapping_1 = (mapping[0] & 0x03) << 6 | (mapping[1] & 0x03) << 4 | (mapping[2] & 0x3) << 2 | mapping[3] & 0x3
        self.set_dio_mapping_1(mapping_1)
        return self.set_dio_mapping_1(mapping_1)

    @getter(REG.LORA.VERSION)
    def get_version(self, version):
        """ Version code of the chip.
            Bits 7-4 give the full revision number; bits 3-0 give the metal mask revision number.
        :return: Version code
        :rtype: int
        """
        return version

    @getter(REG.LORA.TCXO)
    def get_tcxo(self, tcxo):
        """ Get TCXO or XTAL input setting
            0 -> "XTAL": Crystal Oscillator with external Crystal
            1 -> "TCXO": External clipped sine TCXO AC-connected to XTA pin
        :param tcxo: 1=TCXO or 0=XTAL input setting
        :return: TCXO or XTAL input setting
        :type: int (0 or 1)
        """
        return tcxo & 0b00010000

    @setter(REG.LORA.TCXO)
    def set_tcxo(self, tcxo):
        """ Make TCXO or XTAL input setting.
            0 -> "XTAL": Crystal Oscillator with external Crystal
            1 -> "TCXO": External clipped sine TCXO AC-connected to XTA pin
        :param tcxo: 1=TCXO or 0=XTAL input setting
        :return: new TCXO or XTAL input setting
        """
        return (tcxo >= 1) << 4 | 0x09  # bits 0-3 must be 0b1001

    @getter(REG.LORA.PA_DAC)
    def get_pa_dac(self, pa_dac):
        """ Enables the +20dBm option on PA_BOOST pin
            False -> Default value
            True  -> +20dBm on PA_BOOST when OutputPower=1111
        :return: True/False if +20dBm option on PA_BOOST on/off
        :rtype: bool
        """
        pa_dac &= 0x07  # only bits 0-2
        if pa_dac == 0x04:
            return False
        elif pa_dac == 0x07:
            return True
        else:
            raise RuntimeError("Bad PA_DAC value %s" % hex(pa_dac))

    @setter(REG.LORA.PA_DAC)
    def set_pa_dac(self, pa_dac):
        """ Enables the +20dBm option on PA_BOOST pin
            False -> Default value
            True  -> +20dBm on PA_BOOST when OutputPower=1111
        :param pa_dac: 1/0 if +20dBm option on PA_BOOST on/off
        :return: New pa_dac register value
        :rtype: int
        """
        return 0x87 if pa_dac else 0x84

    def rx_chain_calibration(self, freq=868.):
        """ Run the image calibration (see Semtech documentation section 4.2.3.8)
        :param freq: Frequency for the HF calibration
        :return: None
        """
        # backup some registers
        op_mode_bkup = self.get_mode()
        pa_config_bkup = self.get_register(REG.LORA.PA_CONFIG)
        freq_bkup = self.get_freq()
        # for image calibration device must be in FSK standby mode
        self.set_mode(MODE.FSK_STDBY)
        # cut the PA
        self.set_register(REG.LORA.PA_CONFIG, 0x00)
        # calibration for the LF band
        image_cal = (self.get_register(REG.FSK.IMAGE_CAL) & 0xBF) | 0x40
        self.set_register(REG.FSK.IMAGE_CAL, image_cal)
        while (self.get_register(REG.FSK.IMAGE_CAL) & 0x20) == 0x20:
            pass
        # Set a Frequency in HF band
        self.set_freq(freq)
        # calibration for the HF band
        image_cal = (self.get_register(REG.FSK.IMAGE_CAL) & 0xBF) | 0x40
        self.set_register(REG.FSK.IMAGE_CAL, image_cal)
        while (self.get_register(REG.FSK.IMAGE_CAL) & 0x20) == 0x20:
            pass
        # put back the saved parameters
        self.set_mode(MODE.FSK_STDBY)
        self.set_register(REG.LORA.PA_CONFIG, pa_config_bkup)
        self.set_freq(freq_bkup)

    def get_register(self, register_address):
        """ Returns the value of the register
            we mask the address with 0x7F because we are on read mode -> MSB = 0
        :return: Value of the register
        """

        v = self.spi.transfer(address=register_address & 0x7F)

        return v

    def set_register(self, register_address, val):

        v = self.spi.transfer(address=register_address | 0x80, value=val)

        return v

    def __del__(self):
        self.set_mode(MODE.SLEEP)
        if self.verbose:
            sys.stderr.write("MODE=SLEEP\n")

    def __str__(self):
        # don't use __str__ while in any mode other that SLEEP or STDBY
        assert (self.mode == MODE.SLEEP or self.mode == MODE.STDBY)

        onoff = lambda i: 'ON' if i else 'OFF'
        f = self.get_freq()
        cfg1 = self.get_modem_config_1()
        cfg2 = self.get_modem_config_2()
        cfg3 = self.get_modem_config_3()
        pa_config = self.get_pa_config(convert_dBm=True)
        ocp = self.get_ocp(convert_mA=True)
        lna = self.get_lna()
        s = "SX127x LoRa registers:\n"
        s += " mode               %s\n" % self.get_mode()
        s += " freq               %f MHz\n" % f
        s += " coding_rate        %s\n" % cfg1['coding_rate']
        s += " bw                 %s\n" % cfg1['bw']
        s += " spreading_factor   %s chips/symb\n" % (1 << cfg2['spreading_factor'])
        s += " implicit_hdr_mode  %s\n" % onoff(cfg1['implicit_header_mode'])
        s += " rx_payload_crc     %s\n" % onoff(cfg2['rx_crc'])
        s += " tx_cont_mode       %s\n" % onoff(cfg2['tx_cont_mode'])
        s += " preamble           %d\n" % self.get_preamble()
        s += " low_data_rate_opti %s\n" % onoff(cfg3['low_data_rate_optim'])
        s += " agc_auto_on        %s\n" % onoff(cfg3['agc_auto_on'])
        s += " freq_hop_period    %s\n" % self.get_hop_period()
        s += " hop_channel        %s\n" % self.get_hop_channel()
        s += " payload_length     %s\n" % self.get_payload_length()
        s += " max_payload_length %s\n" % self.get_max_payload_length()
        s += " irq_flags_mask     %s\n" % self.get_irq_flags_mask()
        s += " irq_flags          %s\n" % self.get_irq_flags()
        s += " rx_nb_byte         %d\n" % self.get_rx_nb_bytes()
        s += " pkt_snr_value      %f\n" % self.get_pkt_snr_value()

        return s

    # -- 
    def change_frequency(self, new_freq):
        """Change the frequency on the fly.
        
        :param new_freq: New frequency in MHz
        :type new_freq: float
        """
        # Save the current mode
        current_mode = self.get_mode()

        # Ensure the device is in standby mode before changing frequency
        if current_mode != MODE.STDBY:
            self.set_mode(MODE.STDBY)

        # Set the new frequency
        self.set_freq(new_freq)

        # Restore the previous mode if it was different
        if current_mode != MODE.STDBY:
            self.set_mode(current_mode)

        if self.verbose:
            sys.stderr.write(f"Frequency changed to {new_freq} MHz\n")

    def change_bw(self, new_bw):
        """Change the bandwidth on the fly.
        
        :param new_bw: New bandwidth (0=7.8kHz, 9=500kHz)
        :type new_bw: int
        """
        # Save the current mode
        current_mode = self.get_mode()

        # Ensure the device is in standby mode before changing bandwidth
        if current_mode != MODE.STDBY:
            self.set_mode(MODE.STDBY)

        # Set the new bandwidth
        self.set_bw(new_bw)

        # Restore the previous mode if it was different
        if current_mode != MODE.STDBY:
            self.set_mode(current_mode)

        if self.verbose:
            sys.stderr.write(f"Bandwidth changed to {new_bw}\n")

""" Defines the BOARD class that contains the board pin mappings and RF module HF/LF info. """
import math

from machine import Pin, SPI, reset
import time
from PyLora_SX127x_extensions.board_config import BOARD


class BOARD_ESP32(BOARD):
    """ Board initialisation/teardown and pin configuration is kept here.
        Also, information about the RF module is kept here.
        Pin mapped for Rpi with dragino Hat Lora/GPS v1.4
        Fo reference of mapping see : https://pinout.xyz/pinout/pin7_gpio4
        with :
            https://github.com/dragino/Lora/blob/master/Lora_GPS%20HAT/v1.4/
    """
    # Note that the BCOM numbering for the GPIOs is used.
    DIO_PINS = [26, None, None, None]  # [DIO0, DIO1, DIO2, DIO3]
    CB_DIO0 = None
    CB_DIO1 = None
    CB_DIO2 = None
    CB_DIO3 = None
    NSS = 18  # RasPi GPIO25, connection required!
    RST = 14  # Raspi GPIO17, this is not really required
    LED = 2
    PIN_SCK = 5
    PIN_MOSI = 27
    PIN_MISO = 19

    # The spi object is kept here
    spi = None
    SPI_BUS = 0
    SPI_CS = 0

    # tell pySX127x here whether the attached RF module uses low-band
    # (RF*_LF pins) or high-band (RF*_HF pins).
    # low band (called band 1&2) are 137-175 and 410-525
    # high band (called band 3) is 862-1020
    low_band = True


    pin_raised = False

    def __init__(self, led_pin=LED, rst_pin=RST, nss_pin=NSS):
        super().__init__(led_pin, rst_pin, nss_pin)

    def setup_pin(self, pin_num, pin_value=Pin.OUT):
        if pin_num is not None:
            pin = Pin(pin_num, pin_value)
            mock_pin = BOARD.MockC()
            mock_pin.pin_num = pin_num
            mock_pin.value = pin.value

            if pin_value == Pin.OUT:
                mock_pin.low = lambda: pin.value(0)
                mock_pin.high = lambda: pin.value(1)
            else:
                mock_pin.irq = pin.irq

            return mock_pin

    def setup_irq_pin(self, pin_num):
        pin = self.setup_pin(pin_num, Pin.IN)
        if pin:
            pin.set_rising_handler = lambda handler: pin.irq(handler=handler, trigger=Pin.IRQ_RISING)
            pin.detach_irq_trigger = lambda: pin.irq(handler=None, trigger=0)
            return pin

    def init_spi(self):
        try:
            self.spi = SPI(baudrate=5000000, polarity=0, phase=0, bits=8, firstbit=SPI.MSB,
                      sck=Pin(self.PIN_SCK, Pin.OUT, Pin.PULL_DOWN),
                      mosi=Pin(self.PIN_MOSI, Pin.OUT, Pin.PULL_UP),
                      miso=Pin(self.PIN_MISO, Pin.IN, Pin.PULL_UP))
            self.spi.init()

        except Exception as e:
            #print(e)
            if self.spi:
                self.spi.deinit()
                self.spi = None
            reset()  # in case SPI is already in use, need to reset.

        return self.spi

    def get_spi(self):
            new_spi = BOARD.MockC()

            def transfer(address, value=0x00):
                response = bytearray(1)
                self.chip_select(True)
                self.spi.write(bytes([address]))
                self.spi.write_readinto(bytes([value]), response)
                self.chip_select(False)
                return int.from_bytes(response, 'big')

            new_spi.transfer = transfer
            new_spi.close = self.spi.deinit
            return new_spi

    # I need this middle callback to deactivate irq trigger and launch SX127X callback
    def esp32_cb(self, pin_raised):
        self.pin_raised = True
        self.CB_DIO0(None)
        self.dio0_pin.detach_irq_trigger()

    def add_event_dio0(self, value=None, blocked=None):
        if self.DIO_PINS[0] is not None:
            self.dio0_pin.detach_irq_trigger()
            self.pin_raised = False
            chanel = None
            if blocked is True and value is None or value is 0:
                self.dio0_pin.set_rising_handler(self.esp32_cb)
                while not self.pin_raised:
                    pass
            else:
                if value:
                    chanel = self.settimeout(value=value, callback=self.CB_DIO0)
                else:
                    chanel = self.dio0_pin.set_rising_handler(self.esp32_cb)
            return chanel


    def chip_select(self, value):
        """ Enable/Disable Chip Select via NSS pin
        :return: value
        """
        if self.NSS is not None:
            self.nss_pin.value(not value)
        return value

    def settimeout(self, value, callback):
        value = int(value * 1000) / 1000.0
        # If we adding a timeout , we clear possible events assigned to this pin (DIO0)
        self.dio0_pin.detach_irq_trigger()
        chanel = None
        timer = value*1000
        #print('tt', timer)
        while timer != 0:
            if self.dio0_pin.value() == 1:
                chanel = 1
                break
            timer = timer - 1
            time.sleep(0.001)
        if chanel is None:
            raise BOARD.LoRaTimeoutError("Timeout Exception!")
        else:
            callback(None)
        return chanel

    def __exit__(self):
        self.spi.close()
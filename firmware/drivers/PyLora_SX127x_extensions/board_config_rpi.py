""" Defines the BOARD class that contains the board pin mappings and RF module HF/LF info. """

import RPi.GPIO as GPIO
import spidev

import time

from PyLora_SX127x_extensions.board_config import BOARD

GPIO.setmode(GPIO.BCM)
GPIO.cleanup()

class BOARD_RPI(BOARD):
    """ Board initialisation/teardown and pin configuration is kept here.
        Also, information about the RF module is kept here.
        Pin mapped for Rpi with dragino Hat Lora/GPS v1.4
        Fo reference of mapping see : https://pinout.xyz/pinout/pin7_gpio4
        with :
            https://github.com/dragino/Lora/blob/master/Lora_GPS%20HAT/v1.4/
    """
    # Note that the BCOM numbering for the GPIOs is used.
    DIO_PINS = [4, None, None, None]  # [DIO0, DIO1, DIO2, DIO3]
    CB_DIO0 = None
    CB_DIO1 = None
    CB_DIO2 = None
    CB_DIO3 = None
    NSS = 25
    RST = 17
    LED = None
    PIN_SCK = None
    PIN_MOSI = None
    PIN_MISO = None

    # The spi object is kept here for use on Mock SPI class :)
    spi = None
    SPI_BUS = 0
    SPI_CS = 0

    # tell pySX127x here whether the attached RF module uses low-band
    # (RF*_LF pins) or high-band (RF*_HF pins).
    # low band (called band 1&2) are 137-175 and 410-525
    # high band (called band 3) is 862-1020
    low_band = True

    timeOutBackUp = None

    def __init__(self, led_pin=LED, rst_pin=RST, nss_pin=NSS):
        super().__init__(led_pin, rst_pin, nss_pin)
        GPIO.setwarnings(False)

    def setup_pin(self, pin_num, pin_value=GPIO.OUT):
        if pin_num is not None:
            mock_pin = BOARD.MockC()
            mock_pin.pin_num = pin_num
            mock_pin.value = lambda: GPIO.input(pin_num)
            if pin_value == GPIO.OUT:
                GPIO.setup(pin_num, pin_value)
                mock_pin.low = lambda: GPIO.output(pin_num, 0)
                mock_pin.high = lambda: GPIO.output(pin_num, 1)
            else:
                GPIO.setup(pin_num, pin_value)

            return mock_pin

    def setup_irq_pin(self, pin_num):
        self.chip_select(True)
        pin = self.setup_pin(pin_num, GPIO.IN)
        if pin:
            pin.set_rising_handler = \
                lambda handler: GPIO.add_event_detect(pin.pin_num,
                                                      GPIO.RISING,
                                                      callback=handler)
            pin.detach_irq_trigger = lambda: GPIO.remove_event_detect(pin.pin_num)
            return pin

    def teardown(self):
        """ Cleanup GPIO and SpiDev """
        GPIO.cleanup()
        self.spi.close()

    def get_spi(self):
        new_spi = BOARD.MockC()
        def transfer(address, value=0x00):
            response = bytearray(1)
            self.nss_pin.low()
            response.append(self.spi.xfer2([address, value])[1])
            self.nss_pin.high()
            return int.from_bytes(response, 'big')

        new_spi.transfer = transfer
        new_spi.close = self.spi.close
        return new_spi

    def init_spi(self):
        spi_bus = self.SPI_BUS
        spi_cs = self.SPI_CS
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_cs)
        # SX127x can go up to 10MHz, pick half that to be safe
        self.spi.max_speed_hz = 8000000

        return self.spi

    def add_event_dio0(self, value=None, blocked=None):
        if self.DIO_PINS[0] is not None:

            self.dio0_pin.detach_irq_trigger()
            if blocked is True:
                if value is None:
                    chanel = GPIO.wait_for_edge(self.dio0_pin.pin_num, GPIO.RISING)
                else:
                    chanel = self.settimeout(value=value, callback=self.CB_DIO0)
            else:
                if value is not None:
                    chanel = self.settimeout(value=value, callback=self.CB_DIO0)
                else:
                    chanel = GPIO.add_event_detect(self.dio0_pin.pin_num, GPIO.RISING, callback=self.CB_DIO0)

            if chanel is None and value is not None:
                raise BOARD.LoRaTimeoutError("DIO0 wasnt activated! expcetion thrown")
            else:
                self.CB_DIO0(None)
            return chanel


    def chip_select(self, value):
        """ Enable/Disable Chip Select via NSS pin
        :return: value
        """
        if self.NSS is not None:
            GPIO.output(self.NSS, not value)
        return value

    def settimeout(self, value, callback):
        value = int(value * 1000) / 1000.0
        # If we adding a timeout , we clear possible events assigned to this pin (DIO0)
        self.dio0_pin.detach_irq_trigger()
        chanel = None
        timer = int(value * 1000)
        print('tt', timer)
        chanel = GPIO.wait_for_edge(self.dio0_pin.pin_num, GPIO.RISING, timeout=timer)
        if chanel is None:
            raise BOARD.LoRaTimeoutError("Timeout Exception!")
        else:
            callback(None)
        return chanel

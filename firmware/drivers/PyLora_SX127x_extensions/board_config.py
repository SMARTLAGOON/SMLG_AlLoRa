import time

class BOARD:
    # Clase MOCK para poder crear objetos referenciados
    class MockC:
        pass

    class LoRaTimeoutError(Exception):
        pass

    DIO_PINS = [None, None, None, None]  # [DIO0, DIO1, DIO2, DIO3]
    CB_DIO0 = None
    CB_DIO1 = None
    CB_DIO2 = None
    CB_DIO3 = None
    NSS = None
    RST = None
    LED = None
    PIN_SCK = None
    PIN_MOSI = None
    PIN_MISO = None

    # The spi object is kept here
    spi = None
    SPI_BUS = None
    SPI_CS = None

    # tell pySX127x here whether the attached RF module uses low-band
    # (RF*_LF pins) or high-band (RF*_HF pins).
    # low band (called band 1&2) are 137-175 and 410-525
    # high band (called band 3) is 862-1020
    low_band = True

    def __init__(self, led_pin=LED, rst_pin=RST, nss_pin=NSS):
        self.led_pin = self.setup_pin(led_pin)
        self.reset_pin = self.setup_pin(rst_pin)
        self.nss_pin = self.setup_pin(nss_pin)
        self.dio0_pin = self.setup_irq_pin(self.DIO_PINS[0])
        self.dio1_pin = self.setup_irq_pin(self.DIO_PINS[1])
        self.dio2_pin = self.setup_irq_pin(self.DIO_PINS[2])
        self.dio3_pin = self.setup_irq_pin(self.DIO_PINS[3])
        self.reset(self.reset_pin)

    def setup_pin(self, pin_num, pin_value=None):
        raise NotImplemented()

    def setup_irq_pin(self, pin_num):
        raise NotImplemented()

    def teardown(self):
        raise NotImplemented()

    def init_spi(self):
        raise NotImplemented()

    def add_event_dio0(self, value=None, blocked=None):
        raise NotImplemented()

    def set_irq_callbacks(self, cb_dio0=None, cb_dio1=None, cb_dio2=None, cb_dio3=None):
        self.CB_DIO0 = cb_dio0
        self.CB_DIO1 = cb_dio1
        self.CB_DIO2 = cb_dio2
        self.CB_DIO3 = cb_dio3

    def reset(self, pin):
        pin.low()
        time.sleep(0.05)
        pin.high()
        time.sleep(0.05)

    def chip_select(self, value):
        raise NotImplemented()

    def settimeout(self, value, callback):
        raise NotImplemented()

    def __exit__(self):
        self.spi.close()
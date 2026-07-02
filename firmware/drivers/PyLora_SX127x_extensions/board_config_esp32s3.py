import time
from machine import Pin, SPI, reset
from PyLora_SX127x_extensions.board_config import BOARD

class BOARD_ESP32S3(BOARD):
    # Define pin configuration for ESP32S3
    DIO_PINS = [9, 33, None, 21, 10, 36]  # [DIO0, DIO1, DIO2, DIO3, DIO4, DIO5]
    NSS = 7  # SPI Chip Select
    RST = 8  # Reset Pin
    LED = 37 # On-board LED pin
    PIN_SCK = 5   # SPI Clock
    PIN_MOSI = 6  # SPI Master Out Slave In
    PIN_MISO = 3  # SPI Master In Slave Out

    spi = None  # SPI object
    pin_raised = False

    def __init__(self):
        super().__init__(self.LED, self.RST, self.NSS)
        self.init_spi()
        self.setup_pins()

    def setup_pins(self):
        # Initialize NSS, RST, and LED pins
        self.nss_pin = self.setup_pin(self.NSS)
        self.rst_pin = self.setup_pin(self.RST)
        self.led_pin = self.setup_pin(self.LED)

        # Initialize DIO pins
        self.dio0_pin = self.setup_irq_pin(self.DIO_PINS[0])
        self.dio1_pin = self.setup_irq_pin(self.DIO_PINS[1]) if self.DIO_PINS[1] is not None else None
        self.dio2_pin = self.setup_irq_pin(self.DIO_PINS[2]) if self.DIO_PINS[2] is not None else None
        self.dio3_pin = self.setup_irq_pin(self.DIO_PINS[3]) if self.DIO_PINS[3] is not None else None
        self.dio4_pin = self.setup_irq_pin(self.DIO_PINS[4]) if self.DIO_PINS[4] is not None else None
        self.dio5_pin = self.setup_irq_pin(self.DIO_PINS[5]) if self.DIO_PINS[5] is not None else None

    def setup_pin(self, pin_num, pin_value=Pin.OUT):
        # Setup a single GPIO pin
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
        # Setup an IRQ pin
        pin = self.setup_pin(pin_num, Pin.IN)
        if pin:
            pin.set_rising_handler = lambda handler: pin.irq(handler=handler, trigger=Pin.IRQ_RISING)
            pin.detach_irq_trigger = lambda: pin.irq(handler=None, trigger=0)
            return pin

    def init_spi(self):
        # Initialize the SPI bus
        try:
            self.spi = SPI(baudrate=10000000, polarity=0, phase=0, bits=8, firstbit=SPI.MSB,
                           sck=Pin(self.PIN_SCK, Pin.OUT, Pin.PULL_DOWN),
                           mosi=Pin(self.PIN_MOSI, Pin.OUT, Pin.PULL_UP),
                           miso=Pin(self.PIN_MISO, Pin.IN, Pin.PULL_UP))
            self.spi.init()
        except Exception as e:
            print(e)
            if self.spi:
                self.spi.deinit()
                self.spi = None
            reset()  # Reset if SPI is already in use

    def get_spi(self):
        # Get the configured SPI instance
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

    def chip_select(self, value):
        # Handle the chip select pin for SPI communication
        if self.NSS is not None:
            self.nss_pin.value(not value)

    def esp32s3_cb(self, pin_raised):
        # Callback function for handling interrupts
        self.pin_raised = True
        self.CB_DIO0(None)
        self.dio0_pin.detach_irq_trigger()

    def add_event_dio0(self, value=None, blocked=None):
        # Method to handle DIO0 events
        if self.DIO_PINS[0] is not None:
            self.dio0_pin.detach_irq_trigger()
            self.pin_raised = False
            chanel = None
            if blocked is True and (value is None or value == 0):
                self.dio0_pin.set_rising_handler(self.esp32s3_cb)
                while not self.pin_raised:
                    pass
            else:
                if value:
                    chanel = self.settimeout(value=value, callback=self.CB_DIO0)
                else:
                    chanel = self.dio0_pin.set_rising_handler(self.esp32s3_cb)
            return chanel

    def settimeout(self, value, callback):
        # Timeout method for LoRa operations
        value = int(value * 1000) / 1000.0
        self.dio0_pin.detach_irq_trigger()
        chanel = None
        timer = value * 1000
        while timer != 0:
            if self.dio0_pin.value() == 1:
                chanel = 1
                break
            timer -= 1
            time.sleep(0.001)
        if chanel is None:
            raise BOARD.LoRaTimeoutError("Timeout Exception!")
        else:
            callback(None)
        return chanel

    def __exit__(self):
        # Clean up resources upon exit
        if self.spi:
            self.spi.deinit()

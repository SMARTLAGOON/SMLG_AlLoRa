from machine import Pin, SoftI2C, SPI
import os
from lilygo_oled import OLED

# Base class for T3S3
class T3S3Base:
    # What this board has. The device section of AlLoRa.json says which of these to switch on.
    HAS_SCREEN = True
    HAS_SD = True
    HAS_LED = True

    # Where this board mounts its card. The config names a path (queue_path, a log file); the
    # board is what makes there be a filesystem at it, and the config never learns a pin.
    SD_MOUNT_POINT = "/sd"

    def __init__(self):
        # Define pins for LED, OLED, and SD card
        self.LED = 37
        self.OLED_SDA = 18
        self.OLED_SCL = 17
        self.SD_CS = 13
        self.SD_MOSI = 11
        self.SD_MISO = 2
        self.SD_SCLK = 14

        self.sd = None

        # Initialize helpers
        self.create_helpers()

    def create_helpers(self):
        # Initialize LED and OLED
        self.led = Pin(self.LED, Pin.OUT)
        self.i2c = SoftI2C(scl=Pin(self.OLED_SCL), sda=Pin(self.OLED_SDA))
        self.oled = OLED(self.i2c)

    def mount_sd(self, path=None):
        """Mount the card and return its path. Raises if it cannot; the caller decides if that
        is fatal."""
        from board.sd_manager import SD_manager
        if self.sd is None:
            self.sd = SD_manager(self, path=path)
        return self.sd.get_path()

    def unmount_sd(self):
        if self.sd is not None:
            self.sd.unmount()
            self.sd = None

# T3S3 class
class T3S3(T3S3Base):
    def __init__(self):
        super().__init__()

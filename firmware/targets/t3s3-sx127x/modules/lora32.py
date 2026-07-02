from machine import Pin, SoftI2C, SPI
import os
from lilygo_oled import OLED

# Base class for T3S3
class T3S3Base:
    def __init__(self):
        # Define pins for LED, OLED, and SD card
        self.LED = 37
        self.OLED_SDA = 18
        self.OLED_SCL = 17
        self.SD_CS = 13
        self.SD_MOSI = 11
        self.SD_MISO = 2
        self.SD_SCLK = 14

        # Initialize helpers
        self.create_helpers()

    def create_helpers(self):
        # Initialize LED and OLED
        self.led = Pin(self.LED, Pin.OUT)
        self.i2c = SoftI2C(scl=Pin(self.OLED_SCL), sda=Pin(self.OLED_SDA))
        self.oled = OLED(self.i2c)

# T3S3 class
class T3S3(T3S3Base):
    def __init__(self):
        super().__init__()

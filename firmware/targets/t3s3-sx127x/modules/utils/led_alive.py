import _thread
import time

class LED:
    def __init__(self, t3s3, delta_blink=5, blink_duration=0.5):
        self.led = t3s3.led
        self.delta_blink = delta_blink
        self.blink_duration = blink_duration
        self.alive = True

    def run(self):
        # Start the thread
        _thread.start_new_thread(self.blink, ())

    def blink(self):
        while self.alive:
            self.led.value(1)
            time.sleep(self.blink_duration)
            self.led.value(0)
            time.sleep(self.delta_blink)

    def kill(self):
        self.alive = False
        self.led.value(0)
import utime, sys, gc, ujson, machine
import os
import _thread

from lora32 import T3S3
from utils.oled_screen import OLED_Screen
from utils.led_alive import LED

from AlLoRa.Nodes.Source import Source
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.File import AlLoRa_File
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.time_utils import get_time, sleep

gc.enable()


# Initialize T3S3 and SD_manager
device = T3S3()
led = LED(device)
led.run()

source_layout = [
    {'key': 'MAC', 'pos': {'x': 40, 'y': 0}, 'area': {'x': 40, 'y': 0, 'w': 88, 'h': 12}, 'static': True},
    # {'key': 'File', 'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 88, 'h': 12}},
    {'key': "BW", 'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 30, 'h': 12}},
    {'key': "TX_P", 'pos': {'x': 70, 'y': 12}, 'area': {'x': 70, 'y': 12, 'w': 25, 'h': 12}},
    {'key': "SNR", 'pos': {'x': 95, 'y': 12}, 'area': {'x': 95, 'y': 12, 'w': 30, 'h': 12}},
    {'key': 'RSSI', 'pos': {'x': 40, 'y': 24}, 'area': {'x': 40, 'y': 24, 'w':40, 'h': 12}},
    {'key': 'Chunk', 'pos': {'x': 80, 'y': 24}, 'area': {'x': 80, 'y': 24, 'w': 30, 'h': 12}}, 
    {'key': 'SF', 'pos': {'x': 110, 'y': 24}, 'area': {'x': 110, 'y': 24, 'w': 30, 'h': 12}},
]

with open("AlLoRa_logo.json", "r") as f:
    img_data = ujson.load(f)

screen = OLED_Screen(device, img_data, layout_config=source_layout, button=False)

def run():
    # AlLoRa setup
    sleep(5)
    connector = SX127x_connector()
    lora_node = Source(connector, config_file="LoRa.json")
    chunk_size = lora_node.get_chunk_size() #235
    n = 0

    print("CHUNK SIZE: ", chunk_size)

    lora_node.register_subscriber(screen)
    lora_node.notify_subscribers()
    
    t0 = utime.ticks_ms()
    try:
        lora_node.establish_connection()
        print("Connection OK")
        while True:
            if not lora_node.got_file():
                file = AlLoRa_File(name = 'test_{}.json'.format(n),
                            content = bytearray('{}'.format(0)*1024, 'utf-8'),
                            chunk_size=chunk_size)
                print("Sending LoRa file: ", file.get_name())
                lora_node.set_file(file)
                t_0_send = utime.ticks_ms()
                lora_node.send_file()
                n += 1
                
            utime.sleep(10)

    except Exception as e:
        print(e)
        led.kill()
    print("EXIT")
    #machine.reset() # Reset device

run()
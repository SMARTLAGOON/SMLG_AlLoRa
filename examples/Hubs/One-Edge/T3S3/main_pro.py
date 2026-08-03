# Main for the Hub side of AlLoRa
# HW: T3S3
import time, sys, gc, ujson
import os

from lora32 import T3S3
from utils.sd_manager import SD_manager
from utils.oled_screen import OLED_Screen
from utils.led_alive import LED

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint


#if __name__ == "__main__":
gc.enable()

t3s3 = T3S3()
led = LED(t3s3)
with open("AlLoRa_logo.json", "r") as f:
    img_data = ujson.load(f)

source_layout = [
    {'key': 'MAC', 'pos': {'x': 40, 'y': 0}, 'area': {'x': 40, 'y': 0, 'w': 88, 'h': 12}, 'static': True},
    {'key': 'File', 'pos': {'x': 40, 'y': 12}, 'area': {'x': 40, 'y': 12, 'w': 88, 'h': 12}},
    {'key': 'Signal', 'pos': {'x': 40, 'y': 24}, 'area': {'x': 40, 'y': 24, 'w':40, 'h': 12}},
    {'key': 'Chunk', 'pos': {'x': 80, 'y': 24}, 'area': {'x': 80, 'y': 24, 'w': 40, 'h': 12}}
]

screen = OLED_Screen(t3s3, img_data, button=True, layout_config=source_layout)

connector = SX127x_connector()
lora_node = Hub(connector, config_file = "LoRa.json")

node_test = "X"
noce_mac_address = "42007BC9"   #"da58597c" "75e0604c"   #
node_a = Digital_Endpoint(name=node_test, mac_address=noce_mac_address, active=True)

lora_node.register_subscriber(screen)
lora_node.notify_subscribers()
led.run()

while True:
    file = None
    while file==None:
        file = lora_node.listen_to_endpoint(node_a, 100, print_file=True)
    file_content = file.get_content()
    # Save file to SD
    #sd_manager.create_file(file.get_name(), file_content)
    gc.collect()
    time.sleep(10)
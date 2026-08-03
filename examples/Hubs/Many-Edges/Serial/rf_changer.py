# HW: Raspberry Pi + TTGO Adapter
# Example on how the adapter can ask a node to change its RF configuration:

import RPi.GPIO as GPIO
import time
import  sys, gc

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.Serial_connector import Serial_connector


def reset_esp32():
    RST_PIN = 23  # Or load from configuration
    GPIO.setmode(GPIO.BCM)
    
    try:
        GPIO.setup(RST_PIN, GPIO.OUT)
    except RuntimeError:
        GPIO.cleanup(RST_PIN)  # Cleanup the specific pin if setup fails
        GPIO.setup(RST_PIN, GPIO.OUT)  # Retry setup after cleanup

    GPIO.output(RST_PIN, GPIO.LOW)
    time.sleep(0.1)
    GPIO.output(RST_PIN, GPIO.HIGH)
    print("ESP32 has been reset.")
    GPIO.cleanup()


if __name__ == "__main__":
    reset_esp32()     
    connector = Serial_connector(reset_function=reset_esp32)
    allora_hub = Hub(connector, config_file= "LoRa.json", debug_hops= False)
    # This script does not run the visit loop; it only exercises the RF-config verbs below.
    time.sleep(5)
    # try 3 times: 
    for i in range(3):
        rf_params = connector.get_rf_config()
        if rf_params:
            print("RF configuration: ", rf_params)
            break
        time.sleep(1)
    # try to change the RF configuration for 3 times
    for i in range(3):
        success = connector.change_rf_config(frequency=868, sf=8, bw=125, cr=1, tx_power=14)
        if success:
            print("RF configuration changed.")
            break
        time.sleep(1)
    # check if the RF configuration has been changed successfully
    # try 3 times:
    for i in range(3):
        rf_params = connector.get_rf_config()
        if rf_params:
            print("RF configuration: ", rf_params)
            break
        time.sleep(1)
    #allora_hub.run(print_file_content = True, save_files = True)
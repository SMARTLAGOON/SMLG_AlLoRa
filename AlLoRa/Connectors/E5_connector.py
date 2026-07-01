import utime
from machine import UART
import ubinascii
# import network
import re
from math import ceil

from AlLoRa.Packet import Packet
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print

class E5_connector(Connector):
    def __init__(self):
        super().__init__()

    def config(self, config_json):
        super().config(config_json)
        if self.config_parameters:
            connector_config = self.config_parameters

            self.debug = connector_config.get("debug", True)

            self.uart_id = connector_config.get("uart_id", 1)
            self.baudrate = connector_config.get("baudrate", 115200)  
            self.tx_pin = connector_config.get("tx", 43)
            self.rx_pin = connector_config.get("rx", 44)
            self.bits = connector_config.get("bits", 8)
            self.parity = connector_config.get("parity", None)
            self.stop = connector_config.get("stop", 1)
            self.timeout = connector_config.get("timeout", 300)  # Set the UART timeout to 300 ms
            self.txbuf = connector_config.get("txbuf", 150)  # TX buffer size
            self.rxbuf = connector_config.get("rxbuf", 150)  # RX buffer size
            
            self.tx_preamble = connector_config.get("tx_preamble", 8)
            self.rx_preamble = connector_config.get("rx_preamble", 8)
            
            self.crc = connector_config.get("crc", "ON")
            self.iq = connector_config.get("iq", "OFF")
            self.net = connector_config.get("net", "ON")

            
            # Initialize UART with the default baud rate
            self.uart = UART(self.uart_id, 9600)
            self.uart.init(baudrate=9600, tx=self.tx_pin, rx=self.rx_pin, bits=self.bits, 
                            parity=self.parity, stop=self.stop, timeout=self.timeout,
                            txbuf=self.txbuf, rxbuf=self.rxbuf)
            
            # Set the baud rate to the desired value and restart the module
            self.set_uart_baudrate(self.baudrate)
            self.restart_module()
            # Reinitialize UART with the new baud rate
            self.uart.init(baudrate=self.baudrate, tx=self.tx_pin, rx=self.rx_pin, bits=self.bits, parity=self.parity, stop=self.stop, timeout=self.timeout)
            
            # Set the UART timeout
            self.set_uart_timeout(0)

            # Enter test mode and configure RF settings
            self.enter_test_mode()
            self.set_rf_config(self.frequency, self.sf, self.bw, self.tx_preamble, self.rx_preamble, self.tx_power, self.crc, self.iq, self.net)
            if self.debug:
                print("Configuration: uart_id={}, baudrate={}, tx={}, rx={}, bits={}, parity={}, stop={}, timeout={}".format(
                    self.uart_id, self.baudrate, self.tx_pin, self.rx_pin, self.bits, self.parity, self.stop, self.timeout))
                print("RF Configuration: frequency={}, sf={}, bandwidth={}, tx_preamble={}, rx_preamble={}, tx_power={}, crc={}, iq={}, net={}".format(
                    self.frequency, self.sf, self.bw, self.tx_preamble, self.rx_preamble, self.tx_power, self.crc, self.iq, self.net))
            self.get_mac_from_module()

    def get_mac_from_module(self):
        cmd = "AT+ID\r\n"
        expected_response = "+ID: DevEui,"
        default_APP_EUI = "80:00:00:00:00:00:00:06"
        success, response = self.send_command(cmd, expected_response, 500)
        if success:
            if self.debug:
                print("Response:", response.decode())
            mac = response.decode()#.split('+ID: ')[-1].strip()
            obtained_APP_EUI = mac.split('DevEui, ')[1][:23]
            self.MAC = obtained_APP_EUI.replace(':', '').strip()
            if self.debug:
                print("Obtained APP_EUI:", obtained_APP_EUI)
                print("MAC:", self.MAC)

    def set_dynamic_uart_timeout(self, timeout):
        self.uart.init(baudrate=self.baudrate, tx=self.tx_pin, rx=self.rx_pin, bits=self.bits, parity=self.parity, stop=self.stop, timeout=timeout)
        if self.debug:
            print("UART timeout set to:", timeout)

    # AT Commands
    def send_command(self, cmd, expected_response, timeout_ms):
        start_time_total = utime.ticks_ms()

        self.uart.write(cmd)
        utime.sleep_ms(10)  # Short sleep to ensure command is processed
        
        start_time = utime.ticks_ms()
        response = b''
        while utime.ticks_diff(utime.ticks_ms(), start_time) < timeout_ms:
            if self.uart.any():
                response += self.uart.read()
                if expected_response in response:
                    end_time_total = utime.ticks_ms()
                    if self.debug:
                        print("send_command Total Time:", utime.ticks_diff(end_time_total, start_time_total), "ms")
                    return True, response
        end_time_total = utime.ticks_ms()
        if self.debug:
            print("send_command Total Time:", utime.ticks_diff(end_time_total, start_time_total), "ms")
        return False, response
    
    def set_uart_baudrate(self, baudrate):
        cmd = "AT+UART=BR,{}\r\n".format(baudrate)
        print("Set Baud Rate Command:", cmd)
        expected_response = "+UART=BR"
        success, response = self.send_command(cmd, expected_response, 500)
        if self.debug:
            print("Set Baud Rate Response:", response.decode())
        return success

    def set_uart_timeout(self, timeout):
        cmd = "AT+UART=TIMEOUT,{}\r\n".format(timeout)
        if self.debug:
            print("Set Timeout Command:", cmd)
        expected_response = "+UART: TIMEOUT, {}".format(timeout)
        success, response = self.send_command(cmd, expected_response, 500)
        if self.debug:
            print("Set Timeout Response:", response.decode(), "Success:", success)
        return success

    def restart_module(self):
        # Send a command to reset the module (if available) or reinitialize the UART
        cmd = "AT+RESET\r\n"
        if self.debug:
            print("Restart Module Command:", cmd)
        expected_response = "+RESET"
        success, response = self.send_command(cmd, expected_response, 1000)
        if self.debug:
            print("Restart Module Response:", response.decode())
        utime.sleep(1)  # Wait for the module to reset
        return success

    def enter_test_mode(self):
        cmd = "AT+MODE=TEST\r\n"
        expected_response = "+MODE: TEST"
        success, response = self.send_command(cmd, expected_response, 2000)
        if self.debug:
            print("Enter Test Mode Response:", response.decode())
        return success

    def receive_packet(self, timeout=10000, chunk_size=124):
        cmd = "AT+TEST=RXLRPKT\r\n"
        success = self.uart.write(cmd)
        
        t0_ack = utime.ticks_ms()
        ack = self.uart.read(32)
        start_time_total = utime.ticks_ms()
        response = b''
        
        while utime.ticks_diff(utime.ticks_ms(), start_time_total) < timeout:
            if self.uart.any():
                response += self.uart.read(min(chunk_size, self.uart.any()))
                if response.endswith(b'"\r\n'):
                    break
            else:
                utime.sleep_ms(1)
        
        end_time_total = utime.ticks_ms()
        if self.debug:
            print("receive_packet Total Time:", utime.ticks_diff(end_time_total, start_time_total), "ms")
        
        return response if response else None

    def send_packet(self, data):
        cmd = 'AT+TEST=TXLRPKT,"{}"\r\n'.format(data)
        start_time_total = utime.ticks_ms()
        expected_response = "+TEST: TXLRPKT"    #expected_response = "+TEST: TX DONE\r\n"    #
        sfs = {8: 2, 9: 4, 10: 8, 11: 16, 12: 32}
        sf_changed = False
        if self.sf > 7:
            self.set_dynamic_uart_timeout(ceil((self.timeout * sfs[self.sf])*1.1))
            sf_changed = True

        wt0 = utime.ticks_ms()
        success = self.uart.write(cmd)
        wt1 = utime.ticks_ms()
        print("Time to write:", utime.ticks_diff(wt1, wt0), "ms")
        start_time = utime.ticks_ms()
        #response = b''
        response = self.uart.read()
        if sf_changed:
            self.set_dynamic_uart_timeout(self.timeout) # Set the UART timeout back to the default value
        #expected_response = "+TEST: TXLRPKT"
        
        end_time_total = utime.ticks_ms()
        if self.debug:
            print("send_packet Total Time:", utime.ticks_diff(end_time_total, start_time_total), "ms")
        try: 
            if expected_response in response:
                return True
            else:
                return False
        except:
            return False

    # Packet Handling
    def hex_to_bytes(self, hex_str):
        hex_str = hex_str.replace('"', '').replace('\n', '').replace(' ', '').strip()
        if len(hex_str) % 2 != 0:
            raise ValueError("odd-length string")
        return bytes.fromhex(hex_str)

    def bytes_to_hex(self, byte_str):
        return byte_str.hex()

    def extract_packet_info(self, response):
        # time_0 = utime.ticks_ms()
        rssi_match = re.search(r'RSSI:(-?\d+)', response)
        snr_match = re.search(r'SNR:(-?\d+)', response)

        if rssi_match:
            try:
                self.rssi = int(rssi_match.group(1))
            except ValueError:
                self.rssi = None
        else:
            self.rssi = None

        if snr_match:
            try:
                self.snr = int(snr_match.group(1))
            except ValueError:
                self.snr = None
               
        else:
            self.snr = None
    
    # Connector Methods
    def transmit(self, wire):
        hex_data = self.bytes_to_hex(wire)
        hex_data = hex_data.strip().strip('"')

        success = self.send_packet(hex_data)

        return success

    def recv(self, focus_time=12):
        rt_0 = utime.ticks_ms()
        packet_data = self.receive_packet(focus_time * 1000)
        if packet_data:
            try:
                self.extract_packet_info(packet_data)
                hex_data = packet_data.decode().split('RX ')[-1].strip().strip('"')
                bytes_data = self.hex_to_bytes(hex_data)
                return bytes_data
            except (ValueError, IndexError) as e:
                if self.debug:
                    print("Error decoding packet:", e)
        return None

    def get_rssi(self):
        return self.rssi

    def get_snr(self):
        return self.snr

    def change_rf_config(self, frequency=None, sf=None, bw=None, cr=None, tx_power=None, backup=True):
        if self.debug:
            print("Changing RF Config to: ", frequency, sf, bw, cr, tx_power)
        if backup:
            self.backup_rf_config()
        freq = frequency if frequency is not None else self.frequency
        sf = sf if sf is not None else self.sf
        bandwidth = bw if bw is not None else self.bw
        cr = cr if cr is not None else self.cr
        tx_power = tx_power if tx_power is not None else self.tx_power
        success = self.set_rf_config(freq, sf, bandwidth, self.tx_preamble, self.rx_preamble, tx_power, self.crc, self.iq, self.net)
        if success:
            if self.debug:
                print("RF Config Changed to: ", freq, sf, bandwidth, cr, tx_power)
            self.update_timeouts()
            self.adaptive_timeout = self.max_timeout
            return True
        else:
            self.restore_rf_config()
            return False

    def set_rf_config(self, frequency, sf, bandwidth, tx_preamble, rx_preamble, tx_power, crc, iq, net):
        cmd = "AT+TEST=RFCFG,{},SF{},{},{},{},{},{},{},{}\r\n".format(frequency, sf, bandwidth, tx_preamble, rx_preamble, tx_power, crc, iq, net)
        print(cmd)
        expected_response = "+TEST: RFCFG"
        success, response = self.send_command(cmd, expected_response, 2000)
        if self.debug:
            print("Set RF Config Response:", response.decode())
        if success:
            if self.debug:
                print("RF Config Set to:", frequency, sf, bandwidth, tx_preamble, rx_preamble, tx_power, crc, iq, net)
            self.frequency = frequency
            self.sf = sf
            self.bw = bandwidth
            self.tx_preamble = tx_preamble
            self.rx_preamble = rx_preamble
            self.tx_power = tx_power
            self.crc = crc
            self.iq = iq
            self.net = net
        return success

    def set_frequency(self, frequency):
        success = self.set_rf_config(frequency, "SF" + str(self.sf), self.bw, self.tx_preamble, self.rx_preamble, self.tx_power, self.crc, self.iq, self.net)
        if success:
            if self.debug:
                print("Frequency Set to:", frequency)
        return success

    def set_sf(self, sf):
        success = self.set_rf_config(self.frequency, "SF" + str(self.sf), self.bw, self.tx_preamble, self.rx_preamble, self.tx_power, self.crc, self.iq, self.net)
        if success:
            if self.debug:
                print("SF Changed to:", sf)
        return success

    def set_bw(self, bw):
        success = self.set_rf_config(self.frequency, "SF" + str(self.sf), bw, self.tx_preamble, self.rx_preamble, self.tx_power, self.crc, self.iq, self.net)
        if success:
            if self.debug:
                print("Bandwidth Set to:", bw)
        return success

    def set_cr(self, cr):
        # CR is not configurable in E5 module
        if self.debug:
            print("CR is not configurable in E5 module")
        return False

    def set_transmission_power(self, desired_power):
        success = self.set_rf_config(self.frequency, "SF" + str(self.sf), self.bw, self.tx_preamble, self.rx_preamble, desired_power, self.crc, self.iq, self.net)
        if success:
            if self.debug:
                print("Transmission Power Set to:", desired_power)
        return success
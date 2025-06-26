import json
import serial, struct
from time import sleep, time

from AlLoRa.Packet import Packet
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print

class Serial_connector(Connector):
    MAX_ATTEMPTS = 30  # Maximum attempts before resetting
    RESET_TIMEOUT = 60  # Timeout in seconds before allowing another reset

    def __init__(self, reset_function=None):
        super().__init__()
        self.attempt_count = 0
        self.last_reset_time = 0
        self.reset_function = reset_function

    def config(self, config_json):  #max_timeout = 10
        # JSON Example:
        # {
        #     "name": "N",
        #     "mesh_mode": false,
        #     "debug": false,
        #     "min_timeout": 0.5,
        #     "max_timeout": 6
        #     "serial_port": "/dev/ttyAMA3",
        #     "baud": 9600,
        #     "timeout": 1
        # }
        super().config(config_json)
        if self.config_parameters:
            self.serial_port = self.config_parameters.get('serial_port', "/dev/ttyAMA3")
            self.baud = self.config_parameters.get('baud', 9600)
            self.timeout = self.config_parameters.get('timeout', 1)
            self.serial = serial.Serial(self.serial_port, self.baud, timeout=self.timeout)
            if self.debug:
                print("Serial Connector configure: serial_port: {}, baud: {}, timeout: {}".format(self.serial_port, self.baud, self.timeout))
    
    def send_command(self, command, focus_time=None):
        if focus_time is None:
            focus_time = self.timeout
        # Send command and wait for response
        try:
            self.serial.write(command)
            # Wait for ack response
            print("Command sent: ", command, "timeout: ", self.timeout)
            response = self.serial_receive(focus_time)
            if response is None:  # Check if no response was received
                if self.debug:
                    print("No response received (timeout).")
                raise Exception("No ACK received")
            else:
                self.attempt_count = 0
            return response
        except Exception as e:
            if self.debug:
                print("Error sending command or no response: ", e)
            self.attempt_count += 1
            if self.debug:
                print("Attempt count: ", self.attempt_count, "/", self.MAX_ATTEMPTS) 

            if self.attempt_count >= self.MAX_ATTEMPTS:
                self.attempt_reset()
            else: # If the maximum attempts have not been reached
                if self.debug:
                    print("Max attempts not reached: ", self.attempt_count)

            return None

    def serial_receive(self, focus_time, end_phrase=b"<<END>>\n"):
        start_time = time()
        full_message = bytearray()  # Use a bytearray to accumulate the message
        while True:
            if time() - start_time > focus_time:
                if self.debug:
                    print("Timeout waiting for response.")
                return None  # Return None to indicate a timeout occurred

            line = self.serial.readline()  # Read a line; returns bytes
            if line:
                full_message += line  # Append this line to the full message
                # Check if the end of this line signifies the end of the message
                if full_message.endswith(end_phrase):
                    # Remove the end phrase to return only the message content
                    return full_message[:-len(end_phrase)]
            else:
                sleep(0.01)  # Small delay to avoid hogging the CPU

    def attempt_reset(self):
        if time() - self.last_reset_time > self.RESET_TIMEOUT:
            if self.reset_function is not None:
                if self.debug:
                    print("Resetting...")
                self.reset_function()  # Call the passed-in reset function
                self.attempt_count = 0
                self.last_reset_time = time()
            else:
                if self.debug:
                    print("No reset function provided.")
        else:
            if self.debug:
                print("Reset recently triggered, waiting...")

    def send_and_wait_response(self, packet: Packet):
        binary_payload = packet.get_content()
        encoded_payload = binary_payload.hex().encode()  # Hex seguro para transporte
        command = b"S&W:" + encoded_payload + b"<<END>>\n"  # Append the custom end phrase to the command
        packet_size_sent = len(binary_payload)

        try:
            response = self.send_command(command)
            if not response:
                return {
                    "type": "SEND_ERROR",
                    "message": "No ACK received from serial interface",
                }, packet_size_sent, 0, 0

            if self.debug:
                print("ACK Response: ", response)

            if response.startswith(b"ACK:"):
                # Extract adaptive timeout from the ACK
                try:
                    ack_value = float(response.split(b"ACK:")[1])
                    self.adaptive_timeout = ack_value + 0.5
                except Exception as e:
                    return {
                        "type": "PARSE_ERROR",
                        "message": "Failed to parse ACK: {}".format(e),
                    }, packet_size_sent, 0, 0

                # Wait for the actual response
                focus_time = self.adaptive_timeout
                t0 = time()
                received_data = self.serial_receive(focus_time)
                td = (time() - t0) / 1000  # Calculate the time difference in seconds
                packet_size_received = len(received_data) if received_data else 0

                if received_data:
                    if received_data.startswith(b"ERROR_TYPE:"):
                        parsed_error = self.parse_error_message(received_data)
                        return parsed_error, packet_size_sent, packet_size_received, td

                    response_packet = Packet(self.mesh_mode, self.short_mac)
                    if response_packet.load(received_data):
                        return response_packet, packet_size_sent, packet_size_received, td
                    else:
                        return {
                            "type": "CORRUPTED_PACKET",
                            "message": "Failed to load packet: {}".format(received_data),
                        }, packet_size_sent, packet_size_received, td

                else:
                    return {
                        "type": "TIMEOUT",
                        "message": "No data received within timeout",
                    }, packet_size_sent, 0, td

            else:
                return {
                    "type": "INVALID_ACK",
                    "message": "Unexpected response format: {}".format(response),
                }, packet_size_sent, 0, 0

        except Exception as e:
            return {
                "type": "EXCEPTION",
                "message": "Exception in serial send-and-wait: {}".format(e),
            }, packet_size_sent, 0, 0

    def send(self, packet: Packet):
        packet_content = packet.get_content()
        command = b"Send:" + packet_content.hex().encode() + b"<<END>>\n"  # Append the custom end phrase to the command
        ack_response = self.send_command(command)  # Use send_command to transmit
        if ack_response and b"OK" in ack_response:  # Check if the response contains "OK"
            return True
        else:
            if self.debug:
                print("Send command not acknowledged or error occurred.")
            return False

    def recv(self, focus_time=12): 
        command = b"Listen:" + str(focus_time).encode() + b"<<END>>\n"
        ack_response = self.send_command(command, focus_time=focus_time + 0.5)
        if ack_response and b"OK" in ack_response:
            # Wait for the actual response
            if self.debug:
                print("Listen command acknowledged, waiting for data for {} seconds...".format(focus_time + 0.5))
            received_data = self.serial_receive(focus_time + 0.5)
            if received_data:
                if self.debug:
                    print("Received data: ", received_data)
                return received_data
            else:
                if self.debug:
                    print("No data received")
        else:
            if self.debug:
                print("Listen command not acknowledged or error occurred.")
        return None

    def change_rf_config(self, frequency=None, sf=None, bw=None, cr=None, tx_power=None, backup=True):
        command = b"C_RFC:"
        if frequency:
            command += b"FREQ:" + str(frequency).encode() + b"|"
        if sf:
            command += b"SF:" + str(sf).encode() + b"|"
        if bw:
            command += b"BW:" + str(bw).encode() + b"|"
        if cr:
            command += b"CR:" + str(cr).encode() + b"|"
        if tx_power:
            command += b"TX_POWER:" + str(tx_power).encode() + b"|"
        command += b"<<END>>\n"
        response = self.send_command(command)
        if response and response.startswith(b"OK"):
            return True
        else:
            if self.debug:
                print("Error changing RF config: {}".format(response))

    def get_rf_config(self):
        command = b"GET_RFC:<<END>>\n"
        response = self.send_command(command)
        # Example of expected response format: "FREQ:868000000|SF:12|BW:125|CR:4|TX_POWER:14|<<END>>\n"
        if response and response.startswith(b"FREQ:"):
            params = response.split(b"|")
            # [frequency, sf, bw, cr, tx_power]
            rf_params = []
            for param in params:
                key, value = param.split(b":")
                decoded_key = key.decode("utf-8")
                decoded_value = int(value.decode("utf-8"))
                rf_params.append(decoded_value)
            if len(rf_params) == 5:
                if self.debug:
                    print("RF Config: ", rf_params)
                return rf_params
            else:
                if self.debug:
                    print("Error getting RF config: ", response)
                return []
        else:
            if self.debug:
                print(f"Error getting RF config: {response.decode('utf-8', errors='ignore')}")
        return []
    
    def request_mac(self, retries=5, delay=1):
        command = b"GET_MAC:<<END>>\n"
        
        for attempt in range(retries):
            response = self.send_command(command)
            if response:
                try:
                    decoded_mac = response.decode().strip()
                    # Validamos formato simple: 8 caracteres hex
                    if len(decoded_mac) == 8 and all(c in "0123456789abcdef" for c in decoded_mac.lower()):
                        self.MAC = decoded_mac
                        if self.debug:
                            print("MAC set to:", self.MAC)
                        return self.MAC
                    else:
                        if self.debug:
                            print("Received MAC not valid:", decoded_mac)
                except Exception as e:
                    if self.debug:
                        print("Failed to decode MAC:", e)
            if self.debug:
                print(f"MAC attempt {attempt+1}/{retries} failed. Retrying in {delay} sec...")
            sleep(delay)

        if self.debug:
            print("Failed to get MAC after retries.")
        return "00000000"
            

    def parse_error_message(self, error_data):
        """
        Parse the error string into a dictionary.
        Example format: "ERROR_TYPE:TIMEOUT|MESSAGE:No response received|FOCUS_TIME:1.684544"
        """
        error_str = error_data.decode()  # Convert bytearray to string
        error_dict = {}
        try:
            parts = error_str.split("|")
            for part in parts:
                if ":" in part:
                    key, value = part.split(":", 1)  # Split only on the first ":"
                    error_dict[key.strip()] = value.strip()
        except Exception as e:
            return {
                "type": "PARSE_ERROR",
                "message": f"Failed to parse error message: {e}",
                "raw_data": error_str,
            }
        return error_dict
    

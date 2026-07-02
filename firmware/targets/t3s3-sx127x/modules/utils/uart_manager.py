import machine
import time
import binascii
import gc
import ujson

from utils.OnDemandWriter import OnDemandFileWriter
from utils.OnDemandFile import OnDemandFile

class UART_manager:

    def __init__(self, br=115200, tx=12, rx=34, bits=8, parity=None, stop=1) -> None:
        gc.enable()
        self.br = br
        self.tx = tx
        self.rx = rx
        self.bits = bits
        self.parity = parity
        self.stop = stop
        # set pullup for rx pin
        #machine.Pin(self.rx, machine.Pin.IN, machine.Pin.PULL_UP)
        self.ser = machine.UART(1, baudrate=self.br)
        self.ser.init(baudrate=self.br, tx=self.tx, rx=self.rx,
                        bits=self.bits, parity=self.parity, stop=self.stop, timeout=2000)
        
        self.CHUNK_SIZE = 512

    def read_data(self, path):
        line = self.ser.readline()
        if line:
            line = line.strip()
            # If the line is a "Finished" signal
            if line == b'Finished':
                return "Finished"
            # If the line indicates a JSON file
            elif line.endswith(b'.json'):
                print("JSON file title received: {}".format(line))

                # Get JSON size and checksum
                try:
                    json_size = int(self.ser.read(10))  # Read fixed-size string
                    self.ser.read(1)  # Skip newline character
                    checksum = int(self.ser.read(10))  # Read fixed-size string
                    self.ser.read(1)  # Skip newline character
                    print("JSON size: {}".format(json_size))
                except ValueError:
                    raise ValueError("Invalid JSON size or checksum")

                # Create a new OnDemandFileWriter
                name = line.decode('utf-8')
                writer = OnDemandFileWriter("{}/{}".format(path, name))

                # Read and write JSON data in chunks
                json_data_size = 0
                json_data_checksum = binascii.crc32(b'')

                print("Receiving JSON data...")
                while json_data_size < json_size:
                    chunk = self.ser.read(min(self.CHUNK_SIZE, json_size - json_data_size))
                    # Check if the chunk contains the 'END' signal
                    if chunk:
                        end_index = chunk.find(b'END')
                        if end_index != -1:
                            chunk = chunk[:end_index]
                            print("END signal found in chunk")
                            writer.write(bytes(chunk))
                            json_data_size += len(chunk)
                            json_data_checksum = binascii.crc32(chunk, json_data_checksum)
                            break
                        else:
                            writer.write(bytes(chunk))
                            json_data_size += len(chunk)
                            json_data_checksum = binascii.crc32(chunk, json_data_checksum)
                        print("Received {} bytes".format(json_data_size))
                    else:
                        print("No data received")
                    time.sleep(0.1)  # Wait for 100 milliseconds

                # Close the writer
                writer.close()

                # Check end signal
                line = self.ser.readline()
                if line is None or line.strip() != b'END':
                    raise ValueError("Invalid end signal: {}".format(line))

                # Check size and checksum
                if json_data_size != json_size:
                    raise ValueError("JSON size mismatch")
                if json_data_checksum != checksum:
                    raise ValueError("Checksum mismatch")

                print("JSON received successfully")

                return name

        
    def read_image(self, path, filename):
        line = self.ser.readline()
        if line:
            if line.strip() != b'START':
                raise ValueError("Invalid start signal")

            # Get image's label, size and checksum
            try:
                label = self.ser.readline().decode('utf-8').strip()
                print("Label received: {}".format(label))
                img_size = int(self.ser.read(10))  # Read fixed-size string
                self.ser.read(1)  # Skip newline character
                checksum = int(self.ser.read(10))  # Read fixed-size string
                self.ser.read(1)  # Skip newline character
                print("Image size: {}".format(img_size))
            except ValueError:
                raise ValueError("Invalid image size or checksum")

            # Create a new OnDemandFileWriter
            labeled_filename = "{}/{}-{}.jpeg".format(path, filename, label)
            try:
                print("Creating file: {}".format(labeled_filename))
                
                writer = OnDemandFileWriter(labeled_filename)
                    # Read and write image data in chunks
                img_data_size = 0
                img_data_checksum = binascii.crc32(b'')

                print("Receiving image data...")
                while img_data_size < img_size:
                    chunk = self.ser.read(min(self.CHUNK_SIZE, img_size - img_data_size))
                    # Check if the chunk contains the 'END' signal
                    if chunk:
                        end_index = chunk.find(b'END')
                        if end_index != -1:
                            chunk = chunk[:end_index]
                            print("END signal found in chunk")
                            writer.write(bytes(chunk))
                            img_data_size += len(chunk)
                            img_data_checksum = binascii.crc32(chunk, img_data_checksum)
                            break
                        else:
                            print(chunk)
                            writer.write(bytes(chunk))
                            img_data_size += len(chunk)
                            img_data_checksum = binascii.crc32(chunk, img_data_checksum)
                        print("Received {} bytes".format(img_data_size))
                    else:
                        print("No data received")
                    time.sleep(0.1)  # Wait for 100 milliseconds

                # Close the writer
                writer.close()

                # Check end signal
                line = self.ser.readline()
                if line is None or line.strip() != b'END':
                    raise ValueError("Invalid end signal: {}".format(line))

                # Check size and checksum
                if img_data_size != img_size:
                    raise ValueError("Image size mismatch")
                if img_data_checksum != checksum:
                    raise ValueError("Checksum mismatch")
                
                print("Image received successfully")

                return label    #img_data_size, img_data_checksum
            except Exception as e:
                print("Error creating file: {}".format(e))
                return

    
    def read_zipfile(self, path):
       try:
            writer = None
            while True:

                header = self.ser.readline()
                try:
                    header = header.decode().strip()

                except Exception as e:
                    if header is not None:
                        print("Header: ", header)
                        print("Error decoding header: {}".format(e))
                    header = None

                if header:
                    print(header)

                    if header == 'START':
                        self.ser.write(b'LISTEN\n')
                        self.ser.flush()
                        print("Sent LISTEN")
                        # Get file's label, size and checksum

                        rec = ""
                        size = self.ser.readline(8)
                        rec += str(size) + " - "
                        size = int(size) # read fixed size int
                        print("Size {}".format(size))
                        self.ser.read(1)  # Skip newline character
                        filename = self.ser.read(26)  # Read fixed-size string
                        rec += str(filename.decode('utf-8')) + " - "
                        print("Filename {}".format(filename.decode('utf-8')))
                        self.ser.read(1)  # Skip newline character
                        checksum = int(self.ser.read(10))  # Read fixed-size string
                        rec += str(checksum) + " - "
                        print("Checksum {}".format(checksum))
                        self.ser.read(1)  # Skip newline character
                        print("Metadata filename {}, size: {}, checksum {}".format(filename.decode('utf-8'), size, checksum))
                        filename = filename.decode()
                        #filename = filename[2:4] + filename[5:7] + filename[8:10] + filename[11:13] + filename[-3:]
                        namepath = path + '/' + filename
                        print("Creating file: {}".format(namepath))
                        writer = OnDemandFileWriter(namepath)
                        # Read and write image data in chunks
                        zip_data_size = 0
                        zip_data_checksum = binascii.crc32(b'')

                    elif header == 'END':
                        if zip_data_size != size:
                            raise ValueError("Zip size mismatch {} != {}".format(zip_data_size, size))
                        if zip_data_checksum != checksum:
                            raise ValueError("Checksum mismatch")
                        self.ser.write(b'OK\n')
                        print("Zip received successfully")

                        # Close the writer
                        writer.close()
                        return filename

                    else:
                        # at this point header should contain the checksum for the chunk
                        expected_checksum = int(header)
                        chunk = self.ser.read(min(self.CHUNK_SIZE, size-zip_data_size))
                        received_checksum = binascii.crc32(chunk) & 0xffffffff

                        if received_checksum == expected_checksum:
                            self.ser.write(b'ACK\n')
                            writer.write(bytes(chunk))
                            zip_data_size += len(chunk)
                            zip_data_checksum = binascii.crc32(chunk, zip_data_checksum)

                        else:
                            self.ser.write(b'NACK\n')
                
       except Exception as e:
            print("Error creating file: {}".format(e))
            if writer:
                writer.close()
                # Erase file
                os.remove(namepath)
                gc.collect()
            return None
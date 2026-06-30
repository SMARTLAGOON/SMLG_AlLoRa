import gc
from AlLoRa.Nodes.Node import Node, Packet, urandom
from AlLoRa.File import AlLoRa_File
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep, sleep_ms
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os

class Source(Node):

    def __init__(self, connector, config_file = "LoRa.json"):
        super().__init__(connector, config_file)
        gc.enable()

        max_chunk_size = self.calculate_max_chunk_size()
        if self.chunk_size > max_chunk_size:
            self.chunk_size = max_chunk_size
            if self.debug:
                print("Chunk size too big, setting to max: ", self.chunk_size)

        self.file = None

    def get_chunk_size(self):
        return self.chunk_size

    def got_file(self):     # Check if I have a file to send
        return self.file is not None

    def set_file(self, file : AlLoRa_File):
        self.file = file

    def restore_file(self, file: AlLoRa_File):
        self.set_file(file)
        self.file.first_sent = time()
        self.file.metadata_sent = True

    def send_response(self, response_packet: Packet):
        if response_packet:
            if self.mesh_mode:
                response_packet.set_id(self.generate_id())
            t0 = time()
            if self.connector.sf == 12:
                sleep(1)
            self.send_lora(response_packet)
            tf = time()
            time_send = tf - t0
            time_reply = tf - self.tr
            if self.debug:
                print("Time Send: ", time_send, " Time Reply: ", time_reply)
            if self.subscribers:
                self.status['PSizeS'] = len(response_packet.get_content())
                self.status['TimePS'] = time_send
                self.status['TimeBtw'] = time_reply
                self.notify_subscribers()

    #This function ensures that a received message matches the criteria of any expected message.
    def listen_requester(self):
        packet = Packet(mesh_mode=self.mesh_mode, short_mac=self.short_mac)
        focus_time = self.connector.adaptive_timeout
        t0 = time()
        data = self.connector.recv(focus_time)
        self.tr = time() # Get the time when the packet was received
        td = (self.tr - t0) / 1000  # Calculate the time difference in seconds

        if not data:
            if self.debug:
                print("No data received within focus time")
            
            self.connector.increase_adaptive_timeout()
            return None

        try:
            if not packet.load(data):
                return None
        except Exception as e:
            if data:
                if self.debug:
                    print("Error loading: ", data, " -> ", e)
                self.status["CorruptedPackets"] += 1
            else:
                if self.debug:
                    print("No data received")
            return None

        if self.mesh_mode:
            try:
                packet_id = packet.get_id()  # Check if already forwarded or sent by myself
                if packet_id in self.LAST_SEEN_IDS or packet_id in self.LAST_IDS:
                    if self.debug:
                        print("ALREADY_SEEN", self.LAST_SEEN_IDS)
                    return None
            except Exception as e:
                if self.debug:
                    print(e)

        if self.debug:
            rssi = self.connector.get_rssi()
            snr = self.connector.get_snr()
            print('LISTEN_REQUESTER({}) at: {} || request_content : {}'.format(td, self.connector.adaptive_timeout, packet.get_content()))
            print("RSSI: ", rssi, " SNR: ", snr)
            self.status['RSSI'] = rssi
            self.status['SNR'] = snr
            self.status['PSizeR'] = len(data)
            self.status['TimePR'] = td * 1000  # Time in ms

        self.connector.decrease_adaptive_timeout(td)

        return packet

    def establish_connection(self, try_for=None):
        while True:
            print("Establish")
            new_sf = None
            packet = self.listen_requester()
            if packet:
                if self.is_for_me(packet):
                    command = packet.get_command()
                    if Packet.check_command(command):
                        if command != Packet.OK:
                            return True
                        response_packet = Packet(self.mesh_mode, self.short_mac)
                        response_packet.set_source(self.MAC)
                        response_packet.set_destination(packet.get_source())
                        response_packet.set_ok()

                        if packet.get_change_rf():
                            new_sf = packet.get_config()
                            response_packet.set_change_rf(new_sf)
                        if self.mesh_mode and packet.get_mesh() and packet.get_hop():
                            response_packet.enable_mesh()
                            if not packet.get_sleep():
                                response_packet.disable_sleep()
                        if packet.get_debug_hops():
                            response_packet.add_previous_hops(packet.get_message_path())
                            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)

                        self.send_response(response_packet)
                        
                        if self.subscribers:
                            self.status['Status'] = 'OK'
                            self.notify_subscribers() 

                        if new_sf:
                            response_packet.set_change_rf(new_sf)
                            self.change_rf_config(new_sf)
                                
                        return False
                else:
                    if self.debug:
                        print("Not for me, my mac is: ", self.MAC, " and packet mac is: ", packet.get_destination())
                    self.forward(packet)
            gc.collect()
            if try_for is not None:
                try_for -= 1
                if try_for <= 0:
                    return False

    def send_file(self, timeout=float('inf')):  
        t0 = time() # Start time in ms
        while not self.file.sent:
            packet = self.listen_requester()
            if packet:
                if self.is_for_me(packet=packet):
                    response_packet, new_sf = self.response(packet)
                    self.send_response(response_packet)
                    if new_sf:
                        backup_cks = self.chunk_size
                        self.change_rf_config(new_sf)
                        if self.chunk_size != backup_cks:
                            self.file.change_chunk_size(self.chunk_size)
                else:
                    self.forward(packet=packet)
            elif self.sf_trial:
                self.sf_trial -= 1
                if self.sf_trial <= 0:
                    self.restore_rf_config()
                    self.sf_trial = False

            if time() - t0 > timeout:
                last_sent = self.file.last_chunk_sent 
                del(self.file)
                gc.collect()
                self.file = None
                if self.debug:
                    print("Timeout reached")
                # If something was sent, but not all, we return a True
                if last_sent:
                    return True
                return False 
                    
        del(self.file)
        gc.collect()
        self.file = None
        return True

    def response(self, packet):
        command = packet.get_command()
        if not Packet.check_command(command):
            return None, None

        response_packet = Packet(mesh_mode=self.mesh_mode, short_mac=self.short_mac)
        response_packet.set_source(self.MAC)
        response_packet.set_destination(packet.get_source())

        if self.mesh_mode:
            if packet.get_mesh() and packet.get_hop():
                response_packet.enable_mesh()
                if not packet.get_sleep():
                    response_packet.disable_sleep()

        new_sf = None
        if self.sf_trial:
            if self.debug:
                print("SF Trial ended successfully")
            self.sf_trial = False
            self.backup_config()

        if packet.get_debug_hops():
            response_packet.set_data("")
            response_packet.enable_debug_hops()
            response_packet.add_previous_hops(packet.get_message_path())
            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)
            return response_packet, new_sf

        if command == Packet.CHUNK:
            requested_chunk = int(packet.get_payload().decode())
            response_packet.set_data(self.file.get_chunk(requested_chunk))
            if self.subscribers:
                self.status['Chunk'] = self.file.get_length() - requested_chunk
                self.status['Status'] = 'CHUNK'
                self.status['Retransmission'] = self.file.retransmission

            if self.debug:
                print("RC: {} / {}".format(requested_chunk, self.file.get_length()))

            if not self.file.first_sent:
                self.file.report_SST(True)
            return response_packet, new_sf

        if command == Packet.METADATA:    # handle for new file
            filename = self.file.get_name()
            response_packet.set_metadata(self.file.get_length(), filename)

            if self.file.metadata_sent:
                self.file.retransmission += 1
                if self.debug:
                    print("Asked again for Metadata...")
            else:
                self.file.metadata_sent = True

            if self.subscribers:
                self.status['File'] = filename
                self.status['Status'] = 'Metadata'
                self.status['Chunk'] = self.file.get_length()
                self.status['Retransmission'] = self.file.retransmission
            return response_packet, new_sf

        if command == Packet.OK:
            response_packet.set_ok()

            if packet.get_change_rf():
                new_sf = packet.get_config()
                response_packet.set_change_rf(new_sf)
            elif self.file.first_sent and not self.file.last_sent:	# If some chunks are already sent...
                self.file.sent_ok()
            return response_packet, new_sf

        return response_packet, new_sf

    def forward(self, packet: Packet):
        try:
            if packet.get_mesh():
                if self.debug:
                    print("FORWARDED", packet.get_content())
                
                random_sleep = 0
                if packet.get_sleep():
                    random_sleep = (urandom(1)[0] % 5 + 1) * 0.1
                    
                if packet.get_debug_hops():
                    packet.add_hop(self.name, self.connector.get_rssi(), random_sleep)
                packet.enable_hop()
                if random_sleep:
                    sleep(random_sleep)

                success = self.send_lora(packet)
                if success:
                    self.LAST_SEEN_IDS.append(packet.get_id())
                    self.LAST_SEEN_IDS = self.LAST_SEEN_IDS[-self.MAX_IDS_CACHED:]
                else:
                    if self.debug:
                        print("ALREADY_FORWARDED", self.LAST_SEEN_IDS)
        except Exception as e:
            # If packet was corrupted along the way, won't read the COMMAND part
            if self.debug:
                print("ERROR FORWARDING", e)

import gc
from os import urandom
from AlLoRa.Nodes.Node import Node, Packet
from AlLoRa.Packet_v3 import Packet_v3
from AlLoRa.File import AlLoRa_File
from AlLoRa.utils.time_utils import get_time, current_time_ms as time, sleep_ms
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

    def answer_handshake(self, request):
        """As the Source (handshake initiator), answer the Collector's handshake CTRL: on INIT,
        make a fresh ephemeral key and return the HELLO (its public key); on WELCOME, derive +
        store the session and return the ACK. Returns the reply packet, or None on an
        unexpected message. A fresh ephemeral key per session means a reboot re-handshakes."""
        from AlLoRa.Security.handshake import initiator_hello, initiator_complete
        payload = request.get_payload()
        peer = request.get_source()
        if payload and payload[0] == Node._HS_INIT:
            # A repeated INIT (our HELLO was lost) just makes a fresh ephemeral — the latest
            # one is what the Collector will accept, so the two ends stay in step.
            self._hs_state, hello = initiator_hello(urandom)
            return self._ctrl_packet(peer, Node._HS_HELLO, hello)
        if payload and payload[0] == Node._HS_WELCOME:
            if self._hs_state is not None:
                session = initiator_complete(self._hs_state, payload[1:])
                self.session_store.put(session)
                self._hs_state = None
            # If the state is already cleared, a prior WELCOME completed and its ACK was lost;
            # re-ACK idempotently so the Collector's retransmit still lands.
            return self._ctrl_packet(peer, Node._HS_ACK)
        return None

    def _handshake_responder(self, request):
        # respond()-compatible handler: build the handshake reply and send it.
        self.send_response(self.answer_handshake(request))

    def _respond_handler(self, packet):
        # During a secure transfer the Source may still get a first-contact handshake CTRL
        # (e.g. the Collector re-handshaking); route those to the handshake, data to serving.
        if packet.get_command() == Packet_v3.CTRL:
            self.send_response(self.answer_handshake(packet))
        else:
            self._serve(packet)

    def _serve(self, packet):
        # The Source's responder handler: build the reply for this request, send it, and
        # apply any accepted RF change (after the confirming reply is on the wire).
        response_packet, new_sf = self.response(packet)
        self.send_response(response_packet)
        if new_sf:
            backup_cks = self.chunk_size
            self.change_rf_config(new_sf)
            if self.chunk_size != backup_cks:
                self.file.change_chunk_size(self.chunk_size)

    def send_file(self, timeout=float('inf')):
        t0 = time() # Start time in ms
        while not self.file.sent:
            packet = self.respond(self._respond_handler)
            if packet is None and self.sf_trial:
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

        v3 = self.protocol_version >= 3
        response_packet = self.new_packet()
        if not v3:
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

        if not v3 and packet.get_debug_hops():
            response_packet.set_data("")
            response_packet.enable_debug_hops()
            response_packet.add_previous_hops(packet.get_message_path())
            response_packet.add_hop(self.name, self.connector.get_rssi(), 0)
            return response_packet, new_sf

        if command == Packet.CHUNK:
            requested_chunk = packet.get_chunk_index() if v3 else int(packet.get_payload().decode())
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
            if v3:
                # Typed METADATA: carry chunk_size + total byte length so the receiver's
                # positioned writes stop depending on both ends being configured with the
                # same chunk_size. v2 sent only chunk_count + filename.
                response_packet.set_metadata(self.file.chunk_size, self.file.length, filename)
            else:
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

            if (not v3) and packet.get_change_rf():
                new_sf = packet.get_config()
                response_packet.set_change_rf(new_sf)
            elif self.file.first_sent and not self.file.last_sent:	# If some chunks are already sent...
                self.file.sent_ok()
            return response_packet, new_sf

        return response_packet, new_sf

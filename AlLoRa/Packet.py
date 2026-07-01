import struct
import hashlib
import binascii
try:
    from ujson import loads, dumps
except:
    from json import loads, dumps

class Packet:

    # SM = Short MAC
    HEADER_SIZE_P2P_SM  = 12 #20
    HEADER_FORMAT_P2P_SM  = "!4s4sB3s" #Source, Destination, Flags, Check Sum
    HEADER_SIZE_MESH_SM  = 14  #22
    HEADER_FORMAT_MESH_SM  = "!4s4sB2s3s" #Source, Destination, Flags, ID, Check Sum

    # LM = Long MAC
    HEADER_SIZE_P2P_LM  = 20
    HEADER_FORMAT_P2P_LM  = "!8s8sB3s" #Source, Destination, Flags, Check Sum
    HEADER_SIZE_MESH_LM  = 22
    HEADER_FORMAT_MESH_LM  = "!8s8sB2s3s" #Source, Destination, Flags, ID, Check Sum

    OK = "OK"
    METADATA = "METADATA"  #"request-data-info"
    CHUNK = "CHUNK"                 #"chunk-"
    DATA = "DATA"
    COMMAND = {DATA: "00", OK: "01", CHUNK: "10", METADATA: "11"}
    COMMAND_BITS = {"00": DATA, "01": OK, "10": CHUNK, "11": METADATA}

    @staticmethod
    def check_command(command: str) -> bool:
        if command in Packet.COMMAND:
            return True
        return False

    def __init__(self, mesh_mode, short_mac=False):
        self.mesh_mode = mesh_mode
        self.short_mac = short_mac

        if not self.mesh_mode:
            if self.short_mac:
                self.HEADER_SIZE = self.HEADER_SIZE_P2P_SM
                self.HEADER_FORMAT = self.HEADER_FORMAT_P2P_SM
            else:
                self.HEADER_SIZE = self.HEADER_SIZE_P2P_LM
                self.HEADER_FORMAT = self.HEADER_FORMAT_P2P_LM
            # self.HEADER_SIZE = self.HEADER_SIZE_P2P
            # self.HEADER_FORMAT = self.HEADER_FORMAT_P2P
        else:
            if self.short_mac:
                self.HEADER_SIZE = self.HEADER_SIZE_MESH_SM
                self.HEADER_FORMAT = self.HEADER_FORMAT_MESH_SM
            else:
                self.HEADER_SIZE = self.HEADER_SIZE_MESH_LM
                self.HEADER_FORMAT = self.HEADER_FORMAT_MESH_LM
            # self.HEADER_SIZE = self.HEADER_SIZE_MESH
            # self.HEADER_FORMAT = self.HEADER_FORMAT_MESH

        self.source = ''                  # 8 Bytes mac address of the source
        self.destination = ''             # 8 Bytes mac address of the destination

        self.checksum = None              # Checksum
        self.payload = b''                # Content of the message

        self.check = None                 # True if checksum is correct with content

        ## Flags:
        self.command = None               # Type of command / or Data                           bit: 0, 1
        self.v3_beacon = False            # v3-capable beacon (freed spare)                    bit: 2
        # Only for mesh mode
        self.mesh = False                 # Mesh On or Off for this Node                        bit: 3
        self.sleep = True                # True if should sleep before forwarding message       bit: 4
        self.hop = False                  # If packet was forwarder -> 1, else -> 0             bit: 5
        self.debug_hops = False           # Overrides payload to get path details (hops)        bit: 6
        # Change settings
        self.change_rf = False            # If True, check payload to change SF                 bit: 7

        # For mesh
        self.id = None                    # Random number from 0 to 65.535

        self.content = None

    def __repr__(self):
        return "Packet(mesh_mode={}, source='{}', destination='{}', checksum={}, payload={}, check={}, command={}, mesh={}, sleep={}, hop={}, debug_hops={}, change_rf={}, id={})".format(
            self.mesh_mode, self.source, self.destination, self.checksum, self.payload, self.check, self.command,
            self.mesh, self.sleep, self.hop, self.debug_hops, self.change_rf, self.id)

    def mac_compress(self, mac):
        int_mac = int(mac, 16)  # Convert the hexadecimal segment to an integer
        compressed_source = struct.pack('I', int_mac)  # Compress the value into 4 bytes unsigned int
        return compressed_source

    def mac_decompress(self, compressed_mac):
        decompressed_value = struct.unpack('I', compressed_mac)[0]  # Decompress the 4-byte value back to an unsigned int
        decompressed_hex_value = hex(decompressed_value)  # Convert the integer back to a hexadecimal string
        return str(decompressed_hex_value)[2:10]  # Ensure the string is 8 characters long
    
    def set_source(self, source: str):
        if self.short_mac:
            self.source = self.mac_compress(source)
        else:
            self.source = source

    def replace_source(self, source: str):
        if self.short_mac:
            self.source = self.mac_compress(source)
        else:
            self.source = source.encode()
        
        h = self.build_header()
        self.content = h + self.payload

    def get_source(self):
        if self.short_mac:
            return self.mac_decompress(self.source)
        else:
            return self.source.decode()

    def set_destination(self, destination: str):    # 8 Bytes mac address
        if self.short_mac:
            self.destination = self.mac_compress(destination)
        else:
            self.destination = destination.encode()

    def get_destination(self):
        if self.short_mac:
            return self.mac_decompress(self.destination)
        else:
            return self.destination.decode()

    def get_command(self):
        return self.command

    def set_ok(self):
        self.command = "OK"

    def ask_metadata(self):
         self.command = "METADATA"

    def set_metadata(self, length, name):
        self.command = "METADATA"
        if self.short_mac:
            length_bytes = length.to_bytes(2, 'little')
            name_bytes = name.encode()
            self.payload = length_bytes + name_bytes
        else:
            metadata = {"LENGTH" : length, "FILENAME": name}
            self.payload = dumps(metadata).encode()

    def get_payload(self):
        return self.payload

    def get_metadata(self):
        if self.command == "METADATA":
            try:
                if self.short_mac:
                    length = int.from_bytes(self.payload[:2], 'little')
                    name = self.payload[2:].decode()
                    return {"LENGTH": length, "FILENAME": name}
                else:
                    return loads(self.payload)
            except:
                return None

    def get_config(self):   # RF configuration
        try:
            return loads(self.payload)
        except:
            return None

    def ask_data(self, next_chunk):
        self.command = "CHUNK"
        self.payload = str(next_chunk).encode()

    def set_data(self, chunk):
        self.command = "DATA"
        self.payload = chunk

    def set_v3_beacon(self, on=True):
        # ADR 0003 §5: a v3-capable node sets v2's freed spare bit 2 as a beacon a vanilla
        # v2 peer ignores. Registered/echoing peers upgrade; unknown peers stay v2.
        self.v3_beacon = on

    def get_v3_beacon(self):
        return self.v3_beacon

    def get_mesh(self):
        return self.mesh

    def enable_mesh(self):
        self.mesh = True

    def disable_mesh(self):
        self.mesh = False

    def enable_hop(self):
        self.hop = True

    def get_hop(self):
        return self.hop

    def get_debug_hops(self):
        return self.debug_hops

    def enable_debug_hops(self):
        self.debug_hops = True

    def disable_debug_hops(self):
        self.debug_hops = False

    def enable_sleep(self):
        self.sleep = True

    def disable_sleep(self):
        self.sleep = False

    def get_sleep(self):
        return self.sleep

    def get_change_rf(self):
        return self.change_rf

     # Receives a dictionary with the new configuration
    def set_change_rf(self, rf_config):
        changer = {}
        changer["freq"] = rf_config.get("freq", None)
        changer["sf"] = rf_config.get("sf", None)
        changer["bw"] = rf_config.get("bw", None)
        changer["cr"] = rf_config.get("cr", None)
        changer["tx_power"] = rf_config.get("tx_power", None)
        changer["cks"] = rf_config.get("cks", None)
        # Check if there is any change
        if any(changer.values()):
            self.set_ok()
            self.change_rf = True
            # only send the changes that are not None
            changer = {k: v for k, v in changer.items() if v is not None}
            self.payload = dumps(changer).encode()
            return True
        else:
            return False

    def get_message_path(self):
        if self.debug_hops:
            try:
                return loads(self.payload)
            except:
                return None

    def add_hop(self, name, rssi, time_sleep):
        hop = (name, rssi, time_sleep)
        path = self.get_message_path()
        if isinstance(path, list):
            path.append(hop)
        else:
            path = [hop]
        self.enable_debug_hops()
        self.payload = dumps(path).encode()

    def add_previous_hops(self, path):
        if isinstance(path, list):
            self.payload = dumps(path).encode()

    def set_id(self, id):
        if id <= 65535:
            self.id = id

    def get_id(self):
        return self.id

    def get_length(self):
        if len(self.payload) > 0:
            return self.HEADER_SIZE + len(self.payload)
        else:
            return self.HEADER_SIZE

    def get_checksum(self, data):
        h = hashlib.sha256(data)
        ha = binascii.hexlify(h.digest())
        return (ha[-3:])

    def build_header(self):
        if isinstance(self.source, str):
            self.source = self.source.encode('utf-8')
        if isinstance(self.destination, str):
            self.destination = self.destination.encode('utf-8')

        if self.mesh_mode:
            try:
                id_bytes = self.id.to_bytes(2, 'little')
            except:
                print(self.source, self.destination, self.flags, self.id, self.checksum)
            h = struct.pack(self.HEADER_FORMAT, self.source, self.destination, self.flags, id_bytes, self.checksum)
        else:
            h = struct.pack(self.HEADER_FORMAT, self.source, self.destination, self.flags, self.checksum)
        
        return h


    def close_packet(self):
        if self.command in self.COMMAND:
            command_bits = self.COMMAND[self.command]

            flags = 0
            if command_bits[0] == "1":
                flags = flags | (1<<0)
            if command_bits[1] == "1":
                flags = flags | (1<<1)
            if self.v3_beacon:
                flags = flags | (1<<2)
            if self.mesh:
                flags = flags | (1<<3)
            if self.sleep:
                flags = flags | (1<<4)
            if self.hop:
                flags = flags | (1<<5)
            if self.debug_hops:
                flags = flags | (1<<6)
            if self.change_rf:
                flags = flags | (1<<7)

            self.flags = flags

            p = self.payload
            self.checksum = self.get_checksum(p)

            h = self.build_header()

            self.content = h + self.payload

    def get_content(self):
        if self.content is not None:
            return self.content
        else:
            self.close_packet()
            return self.content

    def parse_flags(self, flags: int):
        c0 = "1" if (flags >> 0) & 1 == 1 else "0"
        c1 = "1" if (flags >> 1) & 1 == 1 else "0"
        self.command = self.COMMAND_BITS[c0+c1]

        self.v3_beacon = (flags >> 2) & 1 == 1
        self.mesh  = (flags >> 3) & 1 == 1
        self.sleep = (flags >> 4) & 1 == 1
        self.hop = (flags >> 5) & 1 == 1
        self.debug_hops = (flags >> 6) & 1 == 1
        self.change_rf = (flags >> 7) & 1 == 1

    def load(self, packet):
        header  = packet[:self.HEADER_SIZE]
        content = packet[self.HEADER_SIZE:]

        if self.mesh_mode:
            self.source, self.destination, flags, id, self.checksum = struct.unpack(self.HEADER_FORMAT, header)
            self.id = int.from_bytes(id, "little")
        else:
            self.source, self.destination, flags, self.checksum = struct.unpack(self.HEADER_FORMAT, header)
        
        self.flags = flags

        self.parse_flags(flags)

        self.payload = content

        self.check = self.checksum == self.get_checksum(self.payload)

        if self.check:
            self.content = packet
        
        return self.check

    def get_dict(self):
        p = self.payload
        self.checksum = self.get_checksum(p)
        d = {"source" : self.source,
            "destination" : self.destination,
            "command" : self.command,
            "checksum" : self.checksum.decode(),
            "payload" : binascii.b2a_base64(self.payload).decode().strip() if self.payload else None,
            "mesh" : self.mesh,
            "hop" : self.hop,
            "sleep" : self.sleep,
            "debug_hops" : self.debug_hops,
            "change_rf" : self.change_rf,
            "id" :self.id,
            }
        return d

    def load_dict(self, d):
        self.source = d["source"]
        self.destination = d["destination"]
        self.command = d["command"]
        self.checksum = d["checksum"].encode()
        self.payload = binascii.a2b_base64(d["payload"]) if d["payload"] else b''
        self.mesh = d["mesh"]
        self.hop = d["hop"]
        self.sleep = d["sleep"]
        self.debug_hops = d["debug_hops"]
        self.change_rf = d["change_rf"]
        self.id = d["id"]

        self.check = self.checksum == self.get_checksum(self.payload)

        if self.check:
            self.close_packet()
        
        return self.check

if __name__ == "__main__":
    mac_address_A = "70b3d5499a76ba3f"[8:]
    mac_address_B = "70b3d54993a5bb9c"[8:]

    mesh_mode = True

    content = b"TEST..."

    s_addr = mac_address_A
    d_addr = mac_address_B

    print("P1: ")
    p = Packet(mesh_mode)
    p.set_source(s_addr)
    p.set_destination(d_addr)

    if mesh_mode:
        id = 555
        #p.set_retransmission(2)
        p.enable_mesh()
        p.enable_hop()
        p.set_id(id)

    #p.ask_data(1500)
    #p.set_data(content)
    p.set_metadata(5, "test")
    packet = p.get_content()
    print("Packet: {}".format(packet))
    print(p.get_payload())

    print("\nP2: ")
    p2 = Packet(mesh_mode)
    successfull = p2.load(packet)
    packet2 = p2.get_content()
    print("Packet loaded: {}".format(packet2))
    print(p2.get_payload())
    js = p2.get_dict()
    print(js)

    print("\nP3: ")
    p3 = Packet(mesh_mode)
    success = p3.load_dict(js)
    print(success)
    packet = p3.get_content()
    print("Packet: {}".format(packet))
    #print(p3.get_payload())
    metadata = p3.get_metadata()
    length = metadata["L"]
    filename = metadata["FN"]
    print(length, filename)

    print(Packet.check_command("OK"))
    print(Packet.check_command("METADATA"))
    print(Packet.check_command("DATA"))
    print(Packet.check_command("CHUNK"))
    print(Packet.check_command("Other"))

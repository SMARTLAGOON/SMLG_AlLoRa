from AlLoRa.File import AlLoRa_File
import time
from AlLoRa.utils.time_utils import get_time
from AlLoRa.utils.debug_utils import print

class Digital_Endpoint:

    REQUEST_DATA_STATE = "REQUEST_DATA_STATE"
    PROCESS_CHUNK_STATE = "PROCESS_CHUNK_STATE"
    OK = "OK"

    def __init__(self, config=None, name="N", mac_address="00000000", active=True, 
                 sleep_mesh=True, asking_frequency=60, listening_time=30, 
                 MAX_RETRANSMISSIONS_BEFORE_MESH=10, lock_on_file_receive=False,
                 max_listen_time_when_locked=300,
                 debug=False):
        """
        Initializes a new Digital Endpoint with detailed control over its operational parameters.

        Parameters:
        - config: Dictionary containing the node configuration.
        - name: The name of the endpoint.
        - mac_address: The MAC address of the endpoint.
        - active: Flag indicating whether the endpoint is active.
        - sleep_mesh: Flag indicating whether the endpoint is in sleep mode in mesh network.
        - asking_frequency: Frequency in seconds at which the gateway should check this endpoint.
        - listening_time: Time in seconds the gateway should focus on this endpoint when checking.
        - MAX_RETRANSMISSIONS_BEFORE_MESH: Maximum retransmissions before enabling mesh mode.
        - lock_on_file_receive: If True, the gateway locks on this node until a complete file is received or a timeout occurs.
        """
        if config:
            self.name = config.get('name', name)
            self.mac_address = config.get('mac_address', mac_address)[-8:]
            self.active = config.get('active', active)
            self.sleep_mesh = config.get('sleep_mesh', sleep_mesh)
            self.asking_frequency = config.get('asking_frequency', asking_frequency)
            self.listening_time = config.get('listening_time', listening_time)
            self.MAX_RETRANSMISSIONS_BEFORE_MESH = config.get('MAX_RETRANSMISSIONS_BEFORE_MESH', MAX_RETRANSMISSIONS_BEFORE_MESH)
            self.lock_on_file_receive = config.get('lock_on_file_receive', lock_on_file_receive)
            self.max_listen_time_when_locked = config.get('max_listen_time_when_locked', max_listen_time_when_locked)
            self.freq = config.get('freq', 868)
            self.sf = config.get('sf', 7)
            self.bw = config.get('bw', 125)
            self.cr = config.get('cr', 1)
            self.tx_power = config.get('tx_power', 14)
        else:
            self.name = name
            self.mac_address = mac_address[-8:]
            self.active = active
            self.sleep_mesh = sleep_mesh
            self.asking_frequency = asking_frequency
            self.listening_time = listening_time
            self.MAX_RETRANSMISSIONS_BEFORE_MESH = MAX_RETRANSMISSIONS_BEFORE_MESH
            self.lock_on_file_receive = lock_on_file_receive
            self.max_listen_time_when_locked = max_listen_time_when_locked
            self.freq = 868
            self.sf = 7
            self.bw = 125
            self.cr = 1
            self.tx_power = 14

        self.state = Digital_Endpoint.OK
        self.current_file = None
        self.file_reception_info = {
            "last_file_name": None,
            "last_file_size": None,
            "last_reception_hour": None,
            "current_receiving_file_name": None,
            "latest_chunk_index": None,
            "total_chunks": None,
            "latest_chunk_reception_time": None
        }
        self.last_checked_time = time.time()  # Track the last time this endpoint was checked.
        self.current_chunk = None
        self.mesh = False  # Mesh mode starts disabled
        self.retransmission_counter = 0  # Counter for retransmissions
        self.debug = debug

    def __repr__(self):
        return "Digital_Endpoint({} ({})".format(self.name, self.mac_address)

    def get_name(self):
        return self.name

    def get_mac_address(self):
        return self.mac_address

    def get_mesh(self):
        return self.mesh

    def is_active(self):
        return self.active

    def reset_state(self):
        self.state = Digital_Endpoint.OK
        self.current_file = None
        self.mesh = False  # Mesh mode starts disabled
        self.retransmission_counter = 0  # Counter for retransmissions

    def enable_mesh(self):
        self.mesh = True
        if self.debug:
            print("Node {}: ENABLING MESH".format(self.name))

    def disable_mesh(self):
        self.mesh = False
        self.retransmission_counter = 0
        if self.debug:
            print("Node {}: DISABLING MESH".format(self.name))

    def get_sleep(self):
        return self.sleep_mesh

    def count_retransmission(self):
        if not self.get_mesh():
            self.retransmission_counter += 1
            if self.retransmission_counter >= self.MAX_RETRANSMISSIONS_BEFORE_MESH:
                self.enable_mesh()

    def reset_retransmission_counter(self, hop): #packet
        if not self.get_mesh():                        # If mesh mode is deactivated and I receive a message from this node
            self.retransmission_counter = 0           # Reset counter, going well...
        else:
            if not hop:
                self.disable_mesh()

    def set_current_file(self, file: AlLoRa_File):
        self.current_file = file

    def get_current_file(self):
        return self.current_file

    def connected(self, ok, hop, mesh_mode):
        if ok:
            if mesh_mode:
                self.reset_retransmission_counter(hop)
            self.state = Digital_Endpoint.REQUEST_DATA_STATE
        else:
            if mesh_mode:
                self.count_retransmission()
        
    def set_metadata(self, metadata, hop, mesh_mode, path=None, chunk_size=None):
        if metadata:
            new_file = AlLoRa_File(name=metadata[1], length=metadata[0], chunk_size=chunk_size, path=path)
            self.set_current_file(new_file)
            self.file_reception_info["current_receiving_file_name"] = new_file.name
            self.file_reception_info["total_chunks"] = new_file.length
            self.file_reception_info["latest_chunk_index"] = None
            if mesh_mode:
                self.reset_retransmission_counter(hop)
            self.state = Digital_Endpoint.PROCESS_CHUNK_STATE
            if self.debug:
                print("Node {}: RECEIVING FILE {}".format(self.name, new_file.name))
        else:
            if mesh_mode:
                self.count_retransmission()

    def get_next_chunk(self):
        try:
            missing_chunks = self.current_file.get_missing_chunks()
            if missing_chunks:
                self.current_chunk = missing_chunks[0]
                return self.current_chunk
            return None
        except Exception as e:
            if self.debug:
                print("Node {}: ERROR IN GET_NEXT_CHUNK: {}".format(self.name, e))
            return None

    def set_data(self, data, hop, mesh_mode):
        if data:
            self.current_file.add_chunk(self.current_chunk, data)
            self.file_reception_info["latest_chunk_index"] = self.current_chunk
            self.file_reception_info["latest_chunk_reception_time"] = get_time()
            if len(self.current_file.get_missing_chunks()) == 0:  # All chunks received
                self.file_reception_info["last_file_name"] = self.current_file.get_name()
                self.file_reception_info["last_file_size"] = self.current_file.get_length()
                self.file_reception_info["last_reception_hour"] = self.file_reception_info["latest_chunk_reception_time"]
                self.state = Digital_Endpoint.OK
                if self.debug:
                    print("Node {}: FILE {} RECEIVED".format(self.name, self.current_file.get_name()))
                return self.current_file
            if mesh_mode:
                self.reset_retransmission_counter(hop)
        else:
            if mesh_mode:
                self.count_retransmission()



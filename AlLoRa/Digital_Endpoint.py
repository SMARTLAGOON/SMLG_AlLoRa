from AlLoRa.File import AlLoRa_File
import time
from AlLoRa.utils.time_utils import get_time
from AlLoRa.utils.debug_utils import print


# The mac_address an endpoint carries when it was registered by anything other than a MAC.
# Not an address: it is the absence of one, and the one label that names no single node.
NO_ADDRESS = "00000000"


def label_for_config(config):
    """The off-air label the endpoint described by a Nodes.json entry would carry.

    The same rule as `Digital_Endpoint.get_label()`, applied to an entry that has not been
    built into one: named by device_id[:4] when it registers a fingerprint, by the short MAC
    otherwise. It is what lets a node find an entry again in the file it read, including the
    inactive entries that never become endpoints at all.
    """
    raw = config.get('device_id')
    if raw is not None:
        device_id = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
        return device_id[:4].hex()
    return config.get('mac_address', NO_ADDRESS)[-8:]


def assign_session_ids(endpoints):
    """Give every endpoint a unique 1-byte sid, resolving device_id[0] clashes.

    Each endpoint keeps its identity-derived sid (device_id[0]) when it is free; on a clash the
    later endpoint is bumped to the lowest free byte, which the Collector then sends in that
    session's WELCOME (the initiator can't derive a reassigned value on its own). Fixed sids
    (an explicit override, or a MAC-registered endpoint with no identity to derive from) are
    reserved first and never moved. A no-op for a single endpoint (the 1:1 Hub case).
    """
    taken = set()
    derived_endpoints = []
    for ep in endpoints:
        d = ep.derived_sid()
        if d is None or ep.session_id != d:   # fixed (override / MAC-registered) -> reserve as-is
            taken.add(ep.session_id)
        else:
            derived_endpoints.append(ep)
    for ep in derived_endpoints:
        sid = ep.derived_sid()
        if sid in taken:
            sid = 0
            while sid in taken and sid < 256:
                sid += 1
            if sid >= 256:                    # 256 live sessions is far beyond any deployment
                sid = ep.derived_sid()        # leave it derived; the store would surface a real clash
        ep.session_id = sid
        taken.add(sid)


class Digital_Endpoint:

    REQUEST_DATA_STATE = "REQUEST_DATA_STATE"
    PROCESS_CHUNK_STATE = "PROCESS_CHUNK_STATE"
    OK = "OK"

    def __init__(self, config=None, name="N", mac_address=NO_ADDRESS, active=True,
                 sleep_mesh=True, asking_frequency=60, listening_time=30,
                 MAX_RETRANSMISSIONS_BEFORE_MESH=10, lock_on_file_receive=False,
                 max_listen_time_when_locked=300,
                 session_id=None,
                 device_id=None,
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

        RF settings are optional. State them in a `connector` block inside `config`, spelled as
        in LoRa.json (`freq`, `sf`, `bandwidth`, `coding_rate`, `tx_power`), so the peer's own
        block can be pasted across; anything left unstated is polled on the config of the node
        holding this endpoint. See `_read_rf`.
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
            self._read_rf(config)
            explicit_sid = config.get('session_id', session_id)
            raw_device_id = config.get('device_id', device_id)
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
            self._read_rf(None)
            explicit_sid = session_id
            raw_device_id = device_id

        # v3 identity: the registered device_id fingerprint. Accepts a hex string
        # (the operator copies it like a MAC) or raw bytes; device_id[:4] addresses first
        # contact and device_id[0] seeds the sid. The sid is that derived byte unless an
        # explicit session_id overrides it (or the Collector reassigns it on a clash).
        self.device_id = bytes.fromhex(raw_device_id) if isinstance(raw_device_id, str) \
            else (bytes(raw_device_id) if raw_device_id is not None else None)
        if explicit_sid is not None:
            self.session_id = explicit_sid
        elif self.device_id is not None:
            self.session_id = self.device_id[0]        # secure: device_id[0]
        elif self.mac_address and self.mac_address != NO_ADDRESS:
            self.session_id = int(self.mac_address[-2:], 16)  # open: short-MAC low byte
        else:
            self.session_id = 0

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

    # The five settings that describe a peer's RADIO: what this node has to match to hear it.
    # Anything else in a pasted block (timeouts, debug, serial_port/baud) describes how the
    # LOCAL node reaches its own radio, so it has no meaning here. The timeouts in particular
    # are already derived: change_rf_config recomputes them from the SF and BW it just set.
    _RF_FIELDS = ("freq", "sf", "bw", "cr", "tx_power")
    # Canonical spelling inside a `connector` block, which is LoRa.json's own: an endpoint
    # block is meant to be that file's connector block, pasted across unedited.
    _RF_FROM_CONNECTOR = {"freq": "freq", "sf": "sf", "bw": "bandwidth",
                          "cr": "coding_rate", "tx_power": "tx_power"}

    def _read_rf(self, config):
        """Read this endpoint's RF from its config, leaving anything unstated as None.

        None means "wherever the node polling me already is", resolved against that node's
        own configured RF when the endpoint is registered. It does NOT mean SF7: an endpoint
        that says nothing about its radio used to retune the poller down to a hardcoded SF7
        that appeared in no config file, so a whole deployment at any other SF went silent
        with nothing logged.

        Two accepted shapes. A `connector` block is the documented one, spelled exactly as in
        LoRa.json so it can be pasted from the peer's own config; the flat `sf`/`bw`/`cr` keys
        are the legacy shape and still read. A block wins outright if both are present.
        """
        self.rf_skipped = ()
        self.rf_source = "node"
        if not config:
            for field in Digital_Endpoint._RF_FIELDS:
                setattr(self, field, None)
            return
        block = config.get('connector')
        if block:
            for field, key in Digital_Endpoint._RF_FROM_CONNECTOR.items():
                setattr(self, field, block.get(key))
            # A pasted block carries the local node's plumbing too. Naming what was skipped
            # turns a silent drop into something visible at boot, where it is cheap.
            self.rf_skipped = tuple(k for k in block
                                    if k not in Digital_Endpoint._RF_FROM_CONNECTOR.values())
            self.rf_source = "connector block"
            return
        for field in Digital_Endpoint._RF_FIELDS:
            setattr(self, field, config.get(field))
        if any(getattr(self, f) is not None for f in Digital_Endpoint._RF_FIELDS):
            self.rf_source = "legacy keys"

    def resolve_rf(self, defaults):
        """Fill whatever this endpoint left unstated from `defaults`, once.

        `defaults` is the polling node's own configured RF, snapshotted at construction and
        NOT read live: by the second visit of a round the radio sits on the previous
        endpoint's config, so resolving against the live values would quietly make every
        unstated endpoint inherit its neighbour instead of the node's own file.

        Returns True when it filled something, so a caller can log the result exactly once.
        """
        filled = False
        for field, value in zip(Digital_Endpoint._RF_FIELDS, defaults):
            if getattr(self, field) is None:
                setattr(self, field, value)
                filled = True
        return filled

    def describe_rf(self):
        return "{}/SF{}/BW{}/CR{}/{}dBm".format(self.freq, self.sf, self.bw,
                                                self.cr, self.tx_power)

    def __repr__(self):
        return "Digital_Endpoint({} ({})".format(self.name, self.get_label())

    def get_name(self):
        return self.name

    def get_label(self):
        # How this endpoint is named OUTSIDE the radio: the results folder, the MQTT topic
        # segment, the status line. Never an address, so nothing on the wire reads it.
        #
        # A device_id-registered endpoint has no MAC to be named by (the operator registers a
        # fingerprint, and v3 never puts a MAC on the wire), so `mac_address` stays at its
        # "00000000" default for every one of them. Naming by that default gave every
        # registered node the SAME folder and topic: two Edges sending `data.txt` overwrote
        # each other, which is precisely the multi-node deployment secure mode exists for.
        # device_id[:4] is the first-contact address, already unique per node, and its hex is
        # 8 characters, the same shape as the short MAC it stands in for.
        if self.device_id is not None:
            return self.device_id[:4].hex()
        return self.mac_address

    def get_mac_address(self):
        return self.mac_address

    def get_did(self):
        # The 4-byte first-contact address (device_id[:4]) for a device_id-registered node, or
        # None for a MAC-registered one, which is what selects the addressing.
        return self.device_id[:4] if self.device_id is not None else None

    def derived_sid(self):
        # The sid this node's identity implies (device_id[0]); None if MAC-registered. Lets the
        # Collector tell an identity-derived sid from a reassigned one (whether to send it).
        return self.device_id[0] if self.device_id is not None else None

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



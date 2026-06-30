import gc
from AlLoRa.Nodes.Node import Node, Packet, urandom, loads, dumps
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.Connector import Connector
from AlLoRa.Interfaces.Interface import Interface
from AlLoRa.utils.time_utils import current_time_ms as time, sleep
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os

class Adapter(Node):

    def __init__(self, connector: Connector, interface: Interface, config_file = "LoRa.json"):
        super().__init__(connector, config_file)
        gc.enable()
        self.sf_trial = None
        self.interface = interface
        self.config_interface()
        self.status["RSSI"] = "-"
        self.status["SNR"] = "-"

    def config_interface(self):
        with open(self.config_file, "r") as f:
            lora_config = loads(f.read())
        config_interface = lora_config['interface']
        self.interface.setup(self.connector, self.debug, config_interface)

    def backup_config(self):
        conf = {"name": self.name,
                "chunk_size": self.chunk_size,
                "mesh_mode": self.mesh_mode,
                "debug": self.debug,
                "connector" : self.connector.backup_config(),
                "interface": self.interface.backup_config()}
        with open(self.config_file, "w") as f:
            f.write(dumps(conf))

    def run(self):
        THREAD_EXIT = False
        while True:
            try:
                if THREAD_EXIT:
                    break
                success = self.interface.client_API()  # change name
                if success:
                    self.status["RSSI"] = self.connector.get_rssi()
                    self.status["SNR"] = self.connector.get_snr()
                    self.notify_subscribers()
                    gc.collect()
                sleep(0.1)

            except KeyboardInterrupt as e:
                THREAD_EXIT = True
                if self.debug:
                    print("THREAD_EXIT")
            except Exception as e:
                if self.debug:
                    print("Error in Adapter: {}".format(e))

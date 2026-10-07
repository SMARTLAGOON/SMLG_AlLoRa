# The same Hub as main.py, but each received file goes to an MQTT broker instead of to disk.
# It imports the adapter lookup from main.py, so copy both files.
# Each file goes out unchanged on `<topic_prefix>/<source>/<filename>`. `topic_for=` changes it.
# A lost final OK can deliver a file twice, so whatever reads the broker must accept duplicates.
# Needs a broker this host can reach and `pip install paho-mqtt` (see ../README.md).

from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Connectors.Serial_connector import Serial_connector
from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink

# Same adapter lookup and same reset as the disk example next door: nothing about the radio
# changes because the files go somewhere else.
from main import find_adapter, reset_adapter


BROKER_HOST = "localhost"
BROKER_PORT = 1883
TOPIC_PREFIX = "allora"


if __name__ == "__main__":
    connector = Serial_connector(reset_function=reset_adapter, port_resolver=find_adapter)
    sink = MQTT_DataSink(host=BROKER_HOST, port=BROKER_PORT, topic_prefix=TOPIC_PREFIX)
    allora_hub = Hub(connector, config_file="LoRa.json", debug_hops=False, data_sink=sink)
    # `save_files=True` still means "a completed file is handed to the sink", not "written to
    # disk": the disk was only ever the default sink's business. With this sink the reassembly
    # temp is published and then discarded, so nothing accumulates under `result_path`.
    allora_hub.run(print_file_content=False, save_files=True)

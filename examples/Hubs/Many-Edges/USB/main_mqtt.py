# The same Hub as `main.py`, with exactly one thing changed: where a received file goes.
#
# `main.py` leaves the default sink in place, so every file that completes lands on this host's
# disk under `result_path`. This one hands each completed file to an MQTT broker instead. The
# radio, the adapter, the USB recovery, the endpoint roster, the keys and the sessions are all
# identical, and that is the point: a `DataSink` is a seam, and changing what happens to a file
# after it arrives touches neither the protocol above it nor the transport below it.
#
# The adapter lookup is imported from `main.py` rather than copied, so the diff between the two
# examples is the sink and nothing else. Copy both files if you take this one.
#
# **Where MQTT belongs, and where it does not.** A broker earns its place on the side that has a
# real network: a Hub on a host, republishing what arrived over the constrained link out to
# whatever consumes it. It is a poor fit as a way of carrying files *over* the LoRa link, where
# an envelope costs airtime on every chunk and a lost message has no retry. AlLoRa is already the
# file-transfer protocol there; MQTT is how the result leaves the gateway.
#
# **What lands on the broker.** Each file is published to `<topic_prefix>/<source>/<filename>`
# with the payload byte-for-byte, so a JSON metric file arrives ready for a telegraf
# mqtt_consumer and an image arrives as raw bytes on its own topic. `<source>` is how the Hub
# names that Edge off the air: its short MAC, or the first four bytes of its device_id once it is
# registered by identity. Pass `topic_for=lambda source, filename: ...` to `MQTT_DataSink` if a
# deployment wants a different tree.
#
# One file reaches the sink exactly when a transfer completes, and delivery is at-least-once: a
# final-OK lost on the air costs a second delivery of the same file, so a sink that writes
# somewhere it should not write twice needs to be idempotent. Publishing on a topic keyed by the
# file's own name already is.
#
# **Requirements.** A broker this host can reach, and paho-mqtt:
#
#     pip install paho-mqtt
#
# To try it against a local one:
#
#     mosquitto -v
#     mosquitto_sub -t 'allora/#' -v          # in another terminal, to watch files land

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

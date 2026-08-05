"""v2 baseline benchmark: Requester (the puller).

Run this against the `v2.0.0` library, not against the v3 branch: it is deliberately frozen
on the v2 API, and `Requester` no longer exists on v3. The harness itself only exists on v3,
so flashing means the tag's `AlLoRa/` package plus this file. See this folder's README.

Continuously pulls files from the Source so the Source can push back-to-back. The
timing/throughput is measured and printed on the *Source* side; this side just keeps
the conversation going. Discards received files (save_file=False).

Set SOURCE_MAC to the Source's MAC (it prints its MAC on boot) and SF to match the
Source's LoRa.json. The endpoint's RF config retunes this node to the Source.
"""
import gc

from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint

SOURCE_MAC = "00000000"   # <-- set to the Source's MAC (printed on the Source's boot)
SF = 7                    # <-- match the Source's LoRa.json sf (run SF7 / SF11 / SF12 in turn)

gc.enable()
connector = SX127x_connector()
node = Requester(connector, config_file="LoRa.json")

endpoint = Digital_Endpoint(config={
    "name": "BenchSrc",
    "mac_address": SOURCE_MAC,
    "active": True,
    "freq": 868,
    "sf": SF,
    "bw": 125,
    "cr": 1,
    "tx_power": 14,
})

print("Pulling from {} at SF{}".format(SOURCE_MAC, SF))
while True:
    node.listen_to_endpoint(endpoint, listening_time=60, save_file=False)
    gc.collect()

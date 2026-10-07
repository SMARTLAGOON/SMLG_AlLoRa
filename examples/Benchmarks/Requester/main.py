"""v2 baseline benchmark: the Requester, which keeps pulling files from the Source.

Run it on the `v2.0.0` library, not on v3 (see this folder's README). The Source measures and
prints the timing. This side only keeps the transfer going and throws away what it receives.
Set SOURCE_MAC to the Source's MAC (printed at boot) and SF to match the Source's LoRa.json.
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

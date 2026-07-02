"""v2 baseline benchmark — Source (the device under test for throughput).

Serves a fixed set of file sizes a few times each and prints one machine-readable
`BENCH,...` line per transfer (wall-clock around send_file -> end-to-end throughput,
including handshake + final OK). Capture this device's serial output to a file and
feed it to ../parse_benchmark.py.

Run one SF per session: set `sf` in this folder's LoRa.json (and match it on the
Requester), flash, capture, then change SF and repeat (e.g. SF7 / SF11 / SF12).
Establishes the v2 baseline the v3 wire-format call is gated on (measured, not arithmetic).
"""
import gc

from AlLoRa.Nodes.Source import Source
from AlLoRa.File import AlLoRa_File
from AlLoRa.Connectors.SX127x_connector import SX127x_connector
from AlLoRa.utils.time_utils import current_time_ms as now_ms

ROUNDS = 3                          # repeat the size sweep for averaging
SIZES_KB = [1, 2, 4, 8, 16, 32]     # small files show handshake overhead; big ones, steady state


class RetxProbe:
    """Captures the peak retransmission count for the current file via the status hook."""
    def __init__(self):
        self.retx = 0

    def update(self, status):
        try:
            self.retx = max(self.retx, int(status.get("Retransmission", 0)))
        except Exception:
            pass


gc.enable()
connector = SX127x_connector()
node = Source(connector, config_file="LoRa.json")
cks = node.get_chunk_size()

probe = RetxProbe()
node.register_subscriber(probe)

print("BENCH_HEADER sf={} bw={} cr={} tx={} cks={}".format(
    connector.sf, connector.bw, connector.cr, connector.tx_power, cks))

print("Waiting for the Requester...")
node.establish_connection()
print("Connected — starting sweep")

for _ in range(ROUNDS):
    for kb in SIZES_KB:
        size = kb * 1024
        gc.collect()
        f = AlLoRa_File(name="{}.bin".format(kb), content=bytearray(b"A" * size), chunk_size=cks)
        node.set_file(f)

        probe.retx = 0
        t0 = now_ms()
        ok = node.send_file()           # returns True when the Requester sent the final OK
        sec = (now_ms() - t0) / 1000.0

        print("BENCH,sf={},bw={},cr={},name={}.bin,bytes={},sec={:.3f},retx={},ok={}".format(
            connector.sf, connector.bw, connector.cr, kb, size, sec, probe.retx, 1 if ok else 0))

print("BENCH_DONE")

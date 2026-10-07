"""v2 baseline benchmark: the Source, the board whose throughput is measured.

Run it on the `v2.0.0` library, not on v3 (see this folder's README). It sends a fixed set of
file sizes and prints one `BENCH,...` line per transfer, for ../parse_benchmark.py to read.
Run one SF per session: set `sf` in this folder's LoRa.json, and the same on the Requester.
"""
import gc

from AlLoRa.Nodes.Source import Source
try:
    from AlLoRa.File import CTP_File as BenchFile      # v2.0.0: the library this harness measures
except ImportError:
    from AlLoRa.File import AlLoRa_File as BenchFile   # v3: same class, renamed. Kept so the file
                                                       # also imports on the branch it is stored on
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
        f = BenchFile(name="{}.bin".format(kb), content=bytearray(b"A" * size), chunk_size=cks)
        node.set_file(f)

        probe.retx = 0
        t0 = now_ms()
        ok = node.send_file()           # returns True when the Requester sent the final OK
        sec = (now_ms() - t0) / 1000.0

        print("BENCH,sf={},bw={},cr={},name={}.bin,bytes={},sec={:.3f},retx={},ok={}".format(
            connector.sf, connector.bw, connector.cr, kb, size, sec, probe.retx, 1 if ok else 0))

print("BENCH_DONE")

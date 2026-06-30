# v2 baseline benchmark harness

Measures the **v2 wire's** end-to-end throughput / success / retransmissions on two
T3S3, so the v3 wire-format decision ([ADR 0003](../../../docs/adr/0003-v3-wire-format-and-negotiation.md))
is made against real numbers — the +1-header-byte and DATA-index calls are
**gated on this baseline**, not on arithmetic.

> Run it from the deployed **v2.0.0** tag for a pure v2 baseline (the harness uses
> only public `Source`/`Requester`/`AlLoRa_File` APIs that exist on both v2 and the
> v3 branch). The v3 branch hasn't touched the wire yet, so it reads the same.

## What it does
- **Source** (`Source/main.py`) serves a fixed size sweep (`SIZES_KB`, `ROUNDS` times)
  and prints one machine-readable line per transfer:
  ```
  BENCH,sf=7,bw=125,cr=1,name=8.bin,bytes=8192,sec=12.340,retx=3,ok=1
  ```
  `sec` is wall-clock around `send_file()` (handshake + all chunks + final OK), so
  `bytes*8/sec` is honest end-to-end throughput.
- **Requester** (`Requester/main.py`) just pulls continuously so the Source can push.
- **`parse_benchmark.py`** (laptop, CPython) aggregates the captured `BENCH` lines
  into a per-SF table.

## Run it
1. **Flash** `Source/` to one T3S3, `Requester/` to the other (each with its `LoRa.json`).
2. In `Requester/main.py` set `SOURCE_MAC` (the Source prints its MAC on boot) and `SF`.
3. Pick one SF per session: set `sf` in `Source/LoRa.json` **and** `SF` in `Requester/main.py`
   to the same value. Start the Requester first, then the Source.
4. **Capture the Source's serial** to a file, e.g. with mpremote:
   ```
   mpremote connect /dev/tty.usbserial-XXXX run Source/main.py | tee v2_sf7.log
   ```
   (or `screen` / a pyserial logger). Let it finish (`BENCH_DONE`).
5. Repeat for **SF7, SF11, SF12** (and any BW/CR you care about), one capture file each.
6. **Aggregate:**
   ```
   cat v2_sf7.log v2_sf11.log v2_sf12.log | python3 parse_benchmark.py
   ```
   ```
   SF   BW    CR   files  ok    succ%   mean_bps   median_bps  max_bps    mean_retx
   7    125   1    18     18    100.0   ...        ...         ...        ...
   11   125   1    18     18    100.0   ...        ...         ...        ...
   12   125   1    18     17    94.4    ...        ...         ...        ...
   ```

Keep the capture files — they are the recorded v2 baseline the merge gate compares
against (ADR 0001 acceptance §2).

## Notes
- Both nodes must share the same SF/BW/CR/freq to hear each other; the Requester's
  `Digital_Endpoint` retunes this node to the configured RF.
- `parse_benchmark.py` is pure CPython (no device imports) and is covered by
  `tests/test_benchmark_parser.py`.

# v2 baseline benchmark harness

Measures the **v2 wire's** end-to-end throughput / success / retransmissions on two
T3S3, so the v3 wire-format decision is made against real numbers — the +1-header-byte
and DATA-index calls are **gated on this baseline**, not on arithmetic.

> **Run it against the deployed `v2.0.0` library.** The harness is frozen on the v2 API
> (`Source` / `Requester` / `CTP_File`), which is the point: a v2 baseline has to be
> measured against v2 code, not against v3 code that happens to speak the same wire.
> `Source` and `Requester` were removed from the v3 branch when the aliases went, so
> these two `main.py` files import only against the tag's package. Do not migrate them to
> `Edge` / `Hub`; that would silently turn the baseline into a v3 measurement.
>
> **This folder does not exist at the tag.** It was written later on the v3 branch and
> frozen on the v2 *API*, so "run it from the tag" means: flash the tag's `AlLoRa/`
> package, then add these mains on top. The file class is `CTP_File` at `v2.0.0` and
> `AlLoRa_File` on v3, which is why `Source/main.py` imports it through a try/except.

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
1. **Flash** the `v2.0.0` `AlLoRa/` package to both T3S3, then `Source/` to one and
   `Requester/` to the other (each with its `LoRa.json`).
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
against (v3 must be ≥ v2 before it ships).

## Notes
- Both nodes must share the same SF/BW/CR/freq to hear each other; the Requester's
  `Digital_Endpoint` retunes this node to the configured RF.
- `parse_benchmark.py` is pure CPython (no device imports) and is covered by
  `tests/test_benchmark_parser.py`.

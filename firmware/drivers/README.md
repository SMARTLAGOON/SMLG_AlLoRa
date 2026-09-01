# Chip drivers (vendored)

Low-level radio drivers, vendored here and pinned **with** the protocol so a build never
depends on a floating external module. Each is imported by its matching `Connector`.

| driver | modem | imported by | upstream |
|--------|-------|-------------|----------|
| `PyLora_SX127x_extensions/` | SX127x / SX1276 | `SX127x_connector` (`from PyLora_SX127x_extensions.pyLora import pyLora`) | github.com/GRCDEV/PyLora_SX127x_extensions |
| `sx1262/` | SX126x / SX1262 | `SX1262_connector` (`from sx1262 import SX1262`) | github.com/ehong-tl/micropySX126X, commit `e0f9802` |

**Two shapes, and the build treats them differently.** `PyLora_SX127x_extensions/` is a package:
it has an `__init__.py` and is imported by its directory name, so the build copies the directory.
`sx1262/` is a flat set of modules that import each other by bare name (`sx1262` -> `sx126x` ->
`_sx126x`), so the build copies its `.py` files to the top of the frozen bundle; inside a package
directory none of those imports would resolve. The rule in the workflow is the presence of an
`__init__.py`, so a driver vendored later is handled by which shape it is rather than by a list.

**On the SX1262 copy's provenance.** Three copies were compared before vendoring — the 2023
Heltec bring-up's `lib/`, the upstream archive kept beside it, and the loose copies in the same
folder — and all three were byte-identical to upstream. The long-standing "confirm Abel's exact
changes" caveat is answered: the driver had never been modified before we modified it.

**`sx1262/sx126x.py` is now a fork of `e0f9802`, not a copy of it.** `_sx126x.py` and `sx1262.py`
are still byte-identical. Four changes, all on the path back to receive after a transmission,
which together took that path from about 20 ms to under 5 ms on a T3-S3 and made an SX1262 able
to be a Hub:

| change | why |
|---|---|
| `config()` sets the FS fallback mode rather than STDBY_RC | STDBY_RC stops the crystal, and a board with a TCXO then pays its start-up allowance on the way back: 8.0 ms for one `SetRx` against 1.9 ms from FS |
| `transmit()` no longer ends in `standby()` | it undid the line above; both together are worse than neither |
| `resumeReceive()` is new | `startReceive()` rebuilds a receive configuration that a transmission barely disturbed. Falls back to the full rebuild for an implicit header or a non-LoRa modem |
| `SPItransfer()` sends a command in one transfer, and `_waitBusy()` spins briefly before sleeping | it used to issue one `spi.write`/`spi.read`, and one allocation, per byte, and then sleep a millisecond waiting on a chip busy for microseconds |

The bytes on the bus are unchanged and so is the status decoding. `tests/test_sx126x_driver.py`
covers all four against a fake bus. The first three are ours to keep; the fourth is the one worth
offering upstream.

When updating a driver, re-pin the exact upstream commit and note it here. **Rebasing this one
onto a newer upstream means re-applying the four changes above**, so read this table first.

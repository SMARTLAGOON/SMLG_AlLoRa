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
folder — and all three are byte-identical to upstream. The long-standing "confirm Abel's exact
changes" caveat is answered: the driver was never modified.

When updating a driver, re-pin the exact upstream commit and note it here.

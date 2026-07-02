# Chip drivers (vendored)

Low-level radio drivers, vendored here and pinned **with** the protocol so a build never
depends on a floating external module. Each is imported by its matching `Connector`.

| driver | modem | imported by | upstream |
|--------|-------|-------------|----------|
| `PyLora_SX127x_extensions/` | SX127x / SX1276 | `SX127x_connector` (`from PyLora_SX127x_extensions.pyLora import pyLora`) | github.com/GRCDEV/PyLora_SX127x_extensions |

**Not yet vendored:**
- `sx1262` (micropySX126X) — the SX126x / SX1262 driver `SX1262_connector` imports
  (`from sx1262 import SX1262`). Needed for the `t3s3-sx1262` target; upstream is
  micropySX126X, adapted by Abel for the mistakenly-SX1262 T3S3. Vendor the **confirmed**
  version here as `sx1262/` before enabling that target.

When updating a driver, re-pin the exact upstream commit and note it here.

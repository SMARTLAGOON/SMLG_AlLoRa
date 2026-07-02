# Target: t3s3-sx1262 — LilyGo T3S3 (ESP32-S3) + SX1262  (STUB — not yet buildable)

A secondary target: the T3S3 variant that shipped with an **SX1262** by mistake, which Abel
got working with the micropySX126X driver. Kept as a target so the SX126x path is a first-class
build, not a one-off.

**Blocked on two things before it can build:**
1. **Vendor the driver** — put the confirmed `sx1262` (micropySX126X, with Abel's changes) under
   `firmware/drivers/sx1262/`. This is the "Abel to confirm his exact changes" open item.
2. **Board overlay** — add `boards/ESP32_GENERIC_S3/` (likely close to `t3s3-sx127x`'s, minus
   the SX127x-specific pin/OLED bits) with `MICROPY_PY_CRYPTOLIB_CTR` (+ the legacy
   `MICROPY_PY_UCRYPTOLIB_CTR` for < v1.21) and `require("hmac")` for secure mode.

Then add its row to the workflow matrix (`driver: sx1262`). The device runs `SX1262_connector`
at runtime (`from sx1262 import SX1262`).

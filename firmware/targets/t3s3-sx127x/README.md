# Target: t3s3-sx127x — LilyGo T3S3 (ESP32-S3) + SX127x

The primary board. MicroPython board target `ESP32_GENERIC_S3`, overlaid with this target's
`boards/ESP32_GENERIC_S3/` (4 MB flash, custom partitions, I2C pins for the OLED) and a frozen
bundle: `AlLoRa/` (from the repo) + the `PyLora_SX127x_extensions` driver + the board helpers
in `modules/` (`lora32`, `lilygo_oled`, `utils`).

**Secure mode is enabled in this target:** `mpconfigboard.h` sets `MICROPY_PY_CRYPTOLIB_CTR`
(native AES-CTR — renamed from `MICROPY_PY_UCRYPTOLIB_CTR` in MicroPython v1.21; we define both)
and `manifest.py` does `require("hmac")` (the security layer needs `hmac`, which isn't a
built-in). Without a working CTR mode, `detect_aead()` returns None and a secure node silently
degrades to open — which then can't parse the MAC-addressed handshake at all.

**Runtime:** the device's own `LoRa.json` selects the connector — here `SX127x_connector`,
which drives the `PyLora_SX127x_extensions` chip driver frozen above.

**Versions:** pinned in the workflow matrix row for this target. The original hand-build used
ESP-IDF v5.1.2 + the `patches/network_common.c` WIFI_AUTH fix; the matrix defaults to a newer
pair (patch off) — validate with a `workflow_dispatch` run and fall back if needed.

# Target: t3s3-sx1262 — LilyGo T3S3 (ESP32-S3) + SX1262

The second radio. Same board as [`t3s3-sx127x`](../t3s3-sx127x) — same ESP32-S3, same screen,
same card slot, same pins — carrying an **SX1262** instead of an SX127x. It exists as a target
rather than as a one-off so the SX126x path is a first-class build: `connector.driver` can say
`sx1262` and mean it.

Everything that describes the *board* is byte-identical to the SX127x target's copy, and a test
fails if the two ever stop matching (`tests/test_sx1262_target.py`). Both targets are the same
board; only the radio and the frozen driver differ.

**Runtime:** the device's own `AlLoRa.json` selects the connector through `connector.driver` —
here `SX1262_connector`, which drives the vendored `sx1262` driver
(`from sx1262 import SX1262`).

## The driver

`firmware/drivers/sx1262/` is [micropySX126X](https://github.com/ehong-tl/micropySX126X) at
commit `e0f9802`, vendored **byte-identical to upstream**: the 2023 copy recovered from the
Heltec bring-up, the copy in the upstream archive beside it, and the loose copies in the same
folder are all the same bytes, so there were no local changes to preserve. That settles the
"Abel to confirm his exact changes" item: there were none in the driver.

Unlike the SX127x driver it is a **flat set of modules**, not a package: `sx1262` imports
`sx126x`, which imports `_sx126x`, all by bare name. The build copies its files to the top of
the frozen bundle rather than copying the directory, or none of those imports resolve.

## The pin map is the open item

The connector's pin defaults are the **Heltec LoRa32 V3** map (`clk` 9, `mosi` 10, `miso` 11,
`cs` 8, `irq` 14, `rst` 12, `gpio` 13), because that is the board it was written against in
2023. **The T3S3-with-SX1262 pinout has not been confirmed here**, and nothing in this
repository records it: no config in the tree names SX1262 pins, and the boards on the bench are
all SX127x.

This does not stop the target building, because the pins are not in the firmware. They ride in
the `connector` block of the board's own `AlLoRa.json`, alongside `sf` and `freq`:

```json
"connector": {
  "driver": "sx1262",
  "clk": 5, "mosi": 6, "miso": 3, "cs": 7, "rst": 8, "irq": 33, "gpio": 34,
  "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1, "tx_power": 14
}
```

Those numbers are an illustration, not a verified map. Confirm them against the board in hand
or LilyGo's schematic before flashing, and record the confirmed map here once a board has run.

## Secure mode

Enabled, same as the SX127x target: `mpconfigboard.h` defines `MICROPY_PY_CRYPTOLIB_CTR` (and
the pre-1.21 `MICROPY_PY_UCRYPTOLIB_CTR`) for native AES-CTR, and `manifest.py` does
`require("hmac")`. Without a working CTR mode a secure node degrades to open and cannot parse
the MAC-addressed handshake at all.

## What has not been proven

**No board has run this image.** The build row is enabled and the driver is real, but the
target has never been flashed, so what is verified is that it assembles from files that exist,
not that it transfers a file. The first bench run should confirm, in this order: the board
boots, `SX1262_connector` builds against the pins in its config, a frame goes out, and a retune
moves the radio rather than only the bookkeeping.

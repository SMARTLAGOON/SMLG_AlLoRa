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
commit `e0f9802`. It was vendored byte-identical: the 2023 copy recovered from the Heltec
bring-up, the copy in the upstream archive beside it, and the loose copies in the same folder
were all the same bytes, so there were no local changes to preserve. That settles the "Abel to
confirm his exact changes" item: there were none in the driver.

**`sx126x.py` has since been forked.** Four changes to the path back to receive after a
transmission, without which this board cannot be a Hub: it was deaf for about 20 ms after every
transmission and a peer starts replying in about 9 ms. `_sx126x.py` and `sx1262.py` are still
byte-identical to upstream. The changes, why each one, and what to re-apply on a rebase are in
[`firmware/drivers/README.md`](../../drivers/README.md).

Unlike the SX127x driver it is a **flat set of modules**, not a package: `sx1262` imports
`sx126x`, which imports `_sx126x`, all by bare name. The build copies its files to the top of
the frozen bundle rather than copying the directory, or none of those imports resolve.

## The pin map, confirmed on hardware

The connector's pin defaults are the **Heltec LoRa32 V3** map (`clk` 9, `mosi` 10, `miso` 11,
`cs` 8, `irq` 14, `rst` 12, `gpio` 13), because that is the board it was written against in
2023. **They are not this board's pins.** The map below is, and it has been run.

This does not stop the target building, because the pins are not in the firmware. They ride in
the `connector` block of the board's own `AlLoRa.json`, alongside `sf` and `freq`:

```json
"connector": {
  "driver": "sx1262",
  "clk": 5, "mosi": 6, "miso": 3, "cs": 7, "rst": 8, "irq": 33, "gpio": 34,
  "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1, "tx_power": 14
}
```

**Those numbers are confirmed, not illustrative.** They were read from LilyGo's `utilities.h`
for `LILYGO_S3_E_PAPER_V_1_0` and then run on a LilyGo T3-S3 E-Paper: the radio came up, a
frame went out, and a file transferred. **Do not take this map from LilyGo's wiki page, which
gives a different and wrong LoRa pinout for this variant.** Use the header.

The board also defines a radio power-enable pin at GPIO 35, which this connector never drives.
It does not need to: the radio was brought up with nothing but the seven pins above and
answered with no device errors. The pin is noted here only so the next person to meet a dead
radio on this board does not spend an afternoon on it.

## Secure mode

Enabled, same as the SX127x target: `mpconfigboard.h` defines `MICROPY_PY_CRYPTOLIB_CTR` (and
the pre-1.21 `MICROPY_PY_UCRYPTOLIB_CTR`) for native AES-CTR, and `manifest.py` does
`require("hmac")`. Without a working CTR mode a secure node degrades to open and cannot parse
the MAC-addressed handshake at all.

## What has been proven, and what has not

**A board has now run this image**, on 2026-09-01, and the four things the first bench run was
meant to confirm all held: the board boots, `SX1262_connector` builds against the pins in its
config, a frame goes out, and a retune moves the radio rather than only the bookkeeping. A
config naming `sf` 9, `bandwidth` 250 and `coding_rate` 4 reached the driver as SF9, 250 kHz and
coding-rate denominator 8, none of which is the default a misread key would have produced.

**A mixed pair transfers.** An SX1262 Edge sent a 1000-byte file to an SX127x Hub in five
chunks, byte-exact, with one retransmission. Nothing in the protocol knows which chip is
underneath, and that is now measured rather than assumed.

**What is still unproven on this board:** anything involving the `device` section. The bench
config carried a `connector` block and no `device` section, so no board object was built and
neither the card nor a screen has been exercised here. That is not incidental. The board has an
SPI e-paper display and no OLED, and the shared board file builds an OLED unconditionally, so a
device section would raise inside a display driver before any radio code ran.

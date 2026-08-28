# XIAO ESP32S3 Edge node

A Seeed XIAO ESP32S3 reaching LoRa through a **Wio-E5 AT modem** (`E5_connector`) rather than a
radio it drives directly.

- **main.py**: the simple example. Generates files of increasing size and serves them to the Hub that polls it. Needs only `E5_connector`, which ships with the library, so it runs on any MicroPython build that has AlLoRa frozen in.

The **main_pro.py** that used to sit here is gone, along with its two T3S3 siblings. A board's
screen and card are a `device` block in the config now, read by the same program every v3 node
runs: see [`v3_hello/pro`](../../v3_hello/pro). That program builds a board from a table in
`build_board`, and **the XIAO is not in it yet**, for the same reason the old file could never
boot from this repository:

| module | repository |
|---|---|
| `xiao.py` (`XiaoEsp32S3`) | `AiLoRa`, at `firmware/xiao.py` |
| `lilygo_oled.py`, `board/oled_screen.py`, `board/sd_manager.py`, `board/led_alive.py` | this repo, under `firmware/targets/t3s3-sx127x/modules/` |

There is no `xiao-esp32s3` target under `firmware/targets/`, and the AiLoRa copy of `xiao.py`
depends on a module that lives here. Giving the XIAO its own firmware target would fix both ends
at once: the board file would declare `HAS_SCREEN`, `HAS_SD` and `HAS_LED` like `lora32.py` does,
and adding it to `build_board` would be one import and one branch. Until then, this folder's
`main.py` runs on any MicroPython build with AlLoRa frozen in, and the peripherals do not.

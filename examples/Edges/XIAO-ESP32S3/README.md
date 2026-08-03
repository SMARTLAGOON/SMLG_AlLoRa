# XIAO ESP32S3 Edge node

A Seeed XIAO ESP32S3 reaching LoRa through a **Wio-E5 AT modem** (`E5_connector`) rather than a
radio it drives directly.

- **main.py**: the simple example. Generates files of increasing size and serves them to the Hub that polls it. Needs only `E5_connector`, which ships with the library, so it runs on any MicroPython build that has AlLoRa frozen in.
- **main_pro.py**: the SD-card + OLED version. **It needs board support this repository does not carry.** It imports `xiao` (the `XiaoEsp32S3` pin map) and `utils.subslogger`, and `xiao.py` in turn imports `lilygo_oled`.

Where those live today:

| module | repository |
|---|---|
| `xiao.py` (`XiaoEsp32S3`) | `AiLoRa`, at `firmware/xiao.py` |
| `lilygo_oled.py`, `utils/oled_screen.py`, `utils/sd_manager.py`, `utils/led_alive.py`, `utils/subslogger.py` | this repo, under `firmware/targets/t3s3-sx127x/modules/` |

So neither repository can boot `main_pro.py` on its own: there is no `xiao-esp32s3` target under
`firmware/targets/`, and the AiLoRa copy of `xiao.py` depends on a module that lives here. To run
it today, copy `xiao.py` from AiLoRa onto the board's filesystem along with `lilygo_oled.py` and
the `utils/` package. Giving the XIAO its own firmware target here would make the example
self-contained; that is not built yet.

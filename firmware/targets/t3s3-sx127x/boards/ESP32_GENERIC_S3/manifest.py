include("$(PORT_DIR)/boards/manifest.py")
freeze("modules")           # AlLoRa + the chip driver + board helpers (assembled by CI)
require("ssd1306")          # OLED driver (lilygo_oled)
require("hmac")             # AlLoRa v3 secure mode: kdf.py needs hmac (not built-in). AEAD.py
                            # no longer does: it open-codes RFC 2104 against the native hash,
                            # because this pure-Python wrapper cost it 19.4 ms per frame tag.

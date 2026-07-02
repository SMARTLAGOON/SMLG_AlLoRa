include("$(PORT_DIR)/boards/manifest.py")
freeze("modules")           # AlLoRa + the chip driver + board helpers (assembled by CI)
require("ssd1306")          # OLED driver (lilygo_oled)
require("hmac")             # AlLoRa v3 secure mode: kdf.py + AEAD.py need hmac (not built-in)

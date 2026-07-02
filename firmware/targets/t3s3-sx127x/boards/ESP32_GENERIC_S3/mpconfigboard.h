#ifndef MICROPY_HW_BOARD_NAME
// Can be set by mpconfigboard.cmake.
#define MICROPY_HW_BOARD_NAME               "Generic ESP32S3 module"
#endif
#define MICROPY_HW_MCU_NAME                 "ESP32S3"

#define MICROPY_PY_MACHINE_DAC              (0)

// Enable UART REPL for modules that have an external USB-UART and don't use native USB.
#define MICROPY_HW_ENABLE_UART_REPL         (1)

#define MICROPY_HW_I2C0_SCL                 (9)
#define MICROPY_HW_I2C0_SDA                 (8)

// AlLoRa v3 secure mode: native AES-CTR (uhashlib SHA-256 is already native) so the per-frame
// AEAD runs in C. Without this, detect_aead() finds no backend and a secure node degrades to
// open.
#define MICROPY_PY_UCRYPTOLIB_CTR           (1)

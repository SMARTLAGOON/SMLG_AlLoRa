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

// AlLoRa v3 secure mode: native AES-CTR (hashlib SHA-256 is already native) so the per-frame
// AEAD runs in C. Without a working CTR mode, detect_aead()'s self-test fails, it returns None,
// and a secure node SILENTLY degrades to the open codec — which cannot parse the MAC-addressed
// handshake frames, so the handshake never completes (symptom: endless "Could not parse frame").
//
// MicroPython renamed this flag MICROPY_PY_UCRYPTOLIB_CTR -> MICROPY_PY_CRYPTOLIB_CTR in v1.21
// (the u-module unification). v1.24.1 only honors the new name; the old one is a dead no-op. We
// define BOTH so CTR is enabled regardless of the MicroPython version the build pins.
//
// The same v1.21 change renamed the MODULE ucryptolib -> cryptolib, and (unlike ubinascii/utime)
// it has no weak-link alias, so `import ucryptolib` raises on v1.21+. The library imports it as
// `cryptolib` (AEAD.py) — this flag and that import are the two halves of enabling CTR here:
// with the flag but the old import name, detect_aead() still fails and secure mode degrades.
#define MICROPY_PY_CRYPTOLIB_CTR            (1)   // MicroPython >= 1.21 (the one v1.24.1 checks)
#define MICROPY_PY_UCRYPTOLIB_CTR           (1)   // pre-1.21 name; harmless/ignored on newer builds

# AlLoRa firmware

A board runs AlLoRa on MicroPython with the library built into the firmware, so `import AlLoRa`
works on the device with nothing copied onto it. To put a release's firmware on a board, see
[`T3S3/README.md`](T3S3/README.md). This page is about how that firmware is built.

A **target** is one device with one radio. Its firmware holds the whole `AlLoRa/` folder, that
radio's chip driver, and the board's own helpers (screen, card, LED). The library itself never
knows the chip: each radio is a `Connector` behind the same interface, so only the driver and the
board settings change from one target to the next.

```
firmware/
  drivers/                     chip drivers, copied in and pinned (see drivers/README.md)
    PyLora_SX127x_extensions/  SX127x / SX1276
    sx1262/                    SX126x / SX1262
  targets/                     one folder per device and radio
    t3s3-sx127x/               LilyGo T3S3 with an SX1276
    t3s3-sx1262/               LilyGo T3S3 with an SX1262
      boards/<BOARD>/          MicroPython board files: sdkconfig.board, mpconfigboard.*, manifest.py
      modules/                 the board class and its helpers, built in beside the library
  patches/                     MicroPython source patches for one version (see patches/README.md)
  T3S3/                        how to flash a T3S3
```

## Building

GitHub builds it (`.github/workflows/build-firmware.yml`), inside Espressif's ESP-IDF container,
one job per target, so nothing is installed by hand. Each target's row pins its MicroPython and
ESP-IDF versions, which go together: a MicroPython release supports only some ESP-IDF versions.

Publishing a release runs the build after the tests pass, and once the package is on PyPI it
attaches each image to the release as `AlLoRa-<target>-firmware.bin`
(`.github/workflows/release.yml`). On the default branch it can also be started by hand from the
Actions tab; the images are then on that run's page, for as long as GitHub keeps a run's files.

The build copies `AlLoRa/` as it is at that commit. A change to the library reaches a board only
through a new image, and a board flashed with an older image keeps the older library.

## Adding a target

1. For a new radio, copy its driver into `drivers/<name>/`, pinned to an exact version.
2. Add `targets/<device>-<radio>/` with `boards/<BOARD>/` and `modules/`. For secure mode, turn
   on AES-CTR in `mpconfigboard.h` and add `require("hmac")` to `manifest.py`. The flag is
   `MICROPY_PY_CRYPTOLIB_CTR` from MicroPython v1.21 and `MICROPY_PY_UCRYPTOLIB_CTR` before it;
   define both. Without it a secure node finds no AES and stops at startup with an error.
3. Add a row to the matrix in `build-firmware.yml`: the target folder, board, driver, and the
   MicroPython and ESP-IDF versions.

## Secure mode: what passes on a computer and fails on a board

The tests run on CPython, which has more than MicroPython has, so a call that exists only on
CPython passes every test and then fails on the board. In the secure path that stops the node at
startup. These are the ones found so far, all fixed:

- **`import ucryptolib`**: renamed `cryptolib` in MicroPython v1.21, with no alias. Import
  `cryptolib` first and fall back to `ucryptolib`. (`AlLoRa/Protocol/Security/AEAD.py`)
- **`hmac.compare_digest`**: not in micropython-lib's `hmac`. Use a local constant-time compare.
  (`AlLoRa/Protocol/Security/AEAD.py`)
- **`hashlib.sha256().digest_size`**: MicroPython's hash objects do not have it. Write `32`.
  (`AlLoRa/Protocol/Security/kdf.py`)
- **`int.bit_length()`**: not in MicroPython. The P-256 code reduces the scalar mod N and reads a
  fixed 32 bytes of it instead. (`AlLoRa/Protocol/Security/ec_p256.py`)
- **`hexdigest()`**: not in MicroPython's `hashlib`. Use `digest().hex()`.
  (`AlLoRa/Control/Control_Root_DataSink.py`)
- **`del buf[:n]`**: MicroPython's `bytearray` has no slice deletion. Rebuild the tail
  (`buf = bytearray(buf[n:])`). Deleting a dict key is fine. (`AlLoRa/Connectors/Split/Serial_link.py`)

`tests/test_micropython_portability.py` scans the secure and control files for `bit_length`,
`digest_size` and `compare_digest`, and the whole `AlLoRa/` tree for slice deletion, so those fail
on GitHub rather than on a board. The others are not scanned for.

After a change to the secure path, flash a board and check that the Edge boots without the error
`secure mode requires a crypto (AEAD) backend`; the end of that message names what failed.

Registering a secure node, and what happens when one reboots, are in the main
[README](../README.md).

## Things that only show on a board

- **The first secure contact blocks the radio loop.** The handshake's P-256 math is pure Python:
  about half a second per scalar multiplication on an ESP32, a few seconds in all. It happens once
  per session, never per frame, but keep the handshake's receive window longer than one scalar
  multiplication.
- **A file written whole depends on the flash format.** A node writes its config backup, counters,
  identity key and `Nodes.json` to a temporary file and renames it into place. On littlefs the
  rename replaces the old file. FAT refuses, so the node removes the old file first, and for a
  moment only `Nodes.json.tmp` holds the roster. Nothing recovers that file on boot, so test on the
  card a deployment will use.
- **A USB bridge's console is its link.** On a board used as a modem over its own USB socket, keep
  `mpremote`, `screen` and anything else off that port while it runs: they take the bytes the host
  is waiting for.
- **`modules/board/uart_manager.py` is old.** It is built into both T3S3 targets, but nothing
  imports it, and its blocking read cannot share the radio loop. Use
  `AlLoRa/Data/Sources/Serial_DataSource.py` to take files over a UART.

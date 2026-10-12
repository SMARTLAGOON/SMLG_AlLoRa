# Flashing AlLoRa on a LilyGo T3S3

AlLoRa runs on MicroPython, with the library built into the firmware. Each release carries one
firmware file per radio. Download the one for your board from the latest release on the
[Releases page](https://github.com/SMARTLAGOON/SMLG_AlLoRa/releases):

| Your T3S3 has | Download |
|---|---|
| an SX1276 radio | `AlLoRa-t3s3-sx127x-firmware.bin` |
| an SX1262 radio | `AlLoRa-t3s3-sx1262-firmware.bin` |

Both are built by CI from [`../targets`](../targets) when a release is published.

### 1. Put the board in boot mode

Hold the **BOOT** button while you plug the board in, then release it.

### 2. Find the board's port

```bash
mpremote devs
```

On a Mac the port looks like `/dev/cu.usbmodem1101`, on Linux like `/dev/ttyACM0`.

### 3. Erase the flash and write the firmware

```bash
esptool.py --chip esp32s3 --port PORT erase_flash
esptool.py --chip esp32s3 --port PORT write_flash -z 0x0 AlLoRa-t3s3-sx127x-firmware.bin
```

Replace `PORT` with the port you found, and use the `sx1262` file if that is your radio.

### 4. Restart the board

Unplug the board and plug it back in without the button. The port can change after flashing, so
run `mpremote devs` again.

The [examples](../../examples/Readme.md) take it from there, starting with a first transfer
between two boards.

# AlLoRa examples
---
By [Benjamín Arratia](https://www.barratia.com)

This folder holds examples of AlLoRa on different boards and in different setups, from two boards
on a desk to a Hub on a Raspberry Pi polling many Edges. The best way to learn AlLoRa is to run it,
so we start with the smallest one: one file, from one board to another, over LoRa.

## Your first transfer

This is the long way, every step by hand. The wizard does all of it in one command, from the
repository's root: `python3 tools/provision.py setup` (see the
[Quick start](../README.md#quick-start)).

**Objective:** send a small file from an **Edge** to a **Hub** over LoRa, and find it saved on the
Hub. It takes two boards and about fifteen minutes.

We use the [`v3_hello`](v3_hello) example in its simplest form, [`open/`](v3_hello/open): no keys
and no identities, just the transfer. Every command below runs from the `v3_hello` folder.

### Requirements

#### Hardware

- Two [LilyGo T3S3](https://lilygo.cc/products/t3s3-v1-0?variant=42586879688885) boards, each with
  its antenna attached. We tested in Europe, on 868 MHz. In the US, use the 915 MHz version and set
  `"freq": 915` in both `AlLoRa.json` files.
- Two USB cables.

The T3S3 comes with one of two radios, the SX1276 or the SX1262, and AlLoRa runs on both. This
guide uses the SX1276. With an SX1262, download the `sx1262` firmware below instead, and in both
`AlLoRa.json` files set `"driver": "sx1262"` and add the radio's pins to the `connector` block, as
shown in [the SX1262 firmware notes](../firmware/targets/t3s3-sx1262/README.md).

#### Tools

```bash
pip3 install esptool mpremote
```

`esptool` writes the firmware, and `mpremote` copies files onto the board and opens its console.

#### Firmware

AlLoRa runs on MicroPython, with the library built into the firmware, so there is nothing to copy
for the library itself. Download `AlLoRa-t3s3-sx127x-firmware.bin` (or
`AlLoRa-t3s3-sx1262-firmware.bin`) from the latest release on the
[Releases page](https://github.com/SMARTLAGOON/SMLG_AlLoRa/releases).

### Steps

1. **Find each board's port.** Plug in one board and run:

    ```bash
    mpremote devs
    ```

    On a Mac the port looks like `/dev/cu.usbmodem1101`, on Linux like `/dev/ttyACM0`. Below,
    `EDGE_PORT` and `HUB_PORT` stand for the two ports you find.

2. **Flash the firmware on both boards.** Hold the **BOOT** button while you plug the board in,
   then release it. Then:

    ```bash
    esptool.py --chip esp32s3 --port EDGE_PORT erase_flash
    esptool.py --chip esp32s3 --port EDGE_PORT write_flash -z 0x0 AlLoRa-t3s3-sx127x-firmware.bin
    ```

    Unplug the board, plug it back in without the button, and do the same for the other board. The
    port can change after flashing, so run `mpremote devs` again.

3. **Load the Edge.** The Edge gets the program, its config, and a file to send in its `Outbox`
   folder:

    ```bash
    echo "Hello from the Edge" > hello.txt
    mpremote connect EDGE_PORT fs cp main.py :main.py
    mpremote connect EDGE_PORT fs cp open/edge/AlLoRa.json :AlLoRa.json
    mpremote connect EDGE_PORT fs mkdir Outbox
    mpremote connect EDGE_PORT fs cp hello.txt :Outbox/hello.txt
    ```

4. **Load the Hub.** The Hub gets the same program, its own config, and `Nodes.json`, the list of
   Edges it polls:

    ```bash
    mpremote connect HUB_PORT fs cp main.py :main.py
    mpremote connect HUB_PORT fs cp open/hub/AlLoRa.json :AlLoRa.json
    mpremote connect HUB_PORT fs cp open/hub/Nodes.json :Nodes.json
    ```

    Both boards run the same `main.py`. The `"node"` line in `AlLoRa.json` makes one an Edge and the
    other a Hub.

5. **Start both.** Open a console on each board, in two terminals:

    ```bash
    mpremote connect EDGE_PORT repl
    ```

    Press **Ctrl-D** in each to restart the board, Edge first. To leave the console, press
    **Ctrl-]**.

### What you should see

Among other lines, the Edge prints its settings and waits for the Hub:

```
EDGE ready | MAC: ... | mode: open | session: 42
EDGE serving from: Outbox
```

The Hub prints the Edge it polls, then starts the exchange:

```
HUB ready | MAC: ... | mode: open | endpoints: 1
  polling src as 9eeff0dc for 30 s, then resting 60 s
```

Both have `"debug": true` in their config, so from here they print each step of the exchange. When
the file is complete, the Hub saves it. Leave the console and look for it:

```bash
mpremote connect HUB_PORT fs ls :Results/9eeff0dc
mpremote connect HUB_PORT fs cat :Results/9eeff0dc/hello.txt
```

`9eeff0dc` is the label the Hub gives this Edge, from the `mac_address` in `Nodes.json`. The file is
also gone from the Edge's `Outbox`: the Edge deletes a file only once the Hub has confirmed it.

That is a complete AlLoRa transfer. To send another file, copy it into the Edge's `Outbox`.

### If nothing arrives

- **A board prints `STOPPED:` and a reason.** Its config has a mistake, and the reason says which.
- **The Hub polls but never hears the Edge.** Check that both `AlLoRa.json` files have the same
  `freq`, `sf`, `bandwidth` and `coding_rate`, that both say `"session_id": 42`, and that both
  antennas are on.
- **You want to see every frame.** Set `"debug": true` inside the `connector` block. It is very
  verbose.

## Where to go next

| Example | What it shows |
|---|---|
| [`v3_hello/secure`](v3_hello/secure) | The same transfer, encrypted. Each node gets its own identity. |
| [`v3_hello/control`](v3_hello/control) | Signed commands: the Hub changes the Edge's radio settings over the air. |
| [`v3_hello/pro`](v3_hello/pro) | The board's screen, SD card and LED, switched on from the config. |
| [`v3_hello/serial`](v3_hello/serial) | An Edge whose files come from another device on a cable. |
| [`Hubs/Many-Edges`](Hubs/Many-Edges) | A Hub on a Raspberry Pi or a computer, polling many Edges. |
| [`Adapters`](Adapters) | A radio board for a device with no radio of its own. |
| [`custom_command`](custom_command) | Your own command, signed by the Hub and run beside AlLoRa. |

The [`v3_hello` README](v3_hello/README.md) explains the config file in full: where files come
from, where they go, and what has to match on both boards.

---
#### Using mpremote
<details>
<summary>The few mpremote commands these examples use.</summary>

List the files on the board:
```bash
mpremote connect PORT fs ls
```
Copy a file onto the board, or back from it:
```bash
mpremote connect PORT fs cp main.py :main.py
mpremote connect PORT fs cp :Results/9eeff0dc/hello.txt hello.txt
```
Open the board's console (the MicroPython REPL). **Ctrl-D** restarts the board, **Ctrl-C** stops
the running program, and **Ctrl-]** leaves the console:
```bash
mpremote connect PORT repl
```

We use `mpremote` rather than `ampy`: on a board with native USB, like the T3S3, `ampy` hangs
instead of copying. More in the
[mpremote documentation](https://docs.micropython.org/en/latest/reference/mpremote.html).
</details>

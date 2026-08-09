# v3 hello-world: two T3S3, a file over LoRa

The smallest end-to-end v3 test: one **Edge** serves a 1000-byte file, one **Hub** pulls it. No
`establish_connection()`, just `send_file` / `listen_to_endpoint` running the whole flow, exactly
like the loopback acceptance tests.

It comes in three postures. They are the same transfer with more provisioning underneath it, and
each one is a folder you copy onto two boards as it stands. Start at the top and stop when you
have what you need.

| Folder | What it adds | The Hub registers the Edge by |
|---|---|---|
| [`open/`](open) | Nothing. No identity, no keys, frames in the clear. | a `session_id` you assign to both |
| [`secure/`](secure) | A crypto identity per node: ECDH on first contact, every frame AEAD-sealed. | the `device_id` the Edge derives |
| [`control/`](control) | A fleet control root: the Hub signs configuration commands, the Edge verifies them. | the same `device_id`, plus a shared root |

These used to be one `main.py` per side that branched at boot on whichever config and key files
it found. That is what let one combination, an open node holding a control root, go unrun until
it raised on a bench. Each posture is now its own folder and states what it needs at boot rather
than adapting to what it happens to find.

## Flash and load

All three postures run the same AlLoRa firmware on both boards. Then put one posture's files on
its boards, picking the folder from the table above:

```bash
# Edge device
mpremote connect /dev/cu.usbmodemXXXX fs cp open/edge/main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp open/edge/LoRa.json :LoRa.json

# Hub device
mpremote connect /dev/cu.usbmodemYYYY fs cp open/hub/main.py :main.py
mpremote connect /dev/cu.usbmodemYYYY fs cp open/hub/LoRa.json :LoRa.json
```

Both files land on the board under their plain names, `main.py` and `LoRa.json`, whichever
folder they came from. Nothing on the board records which posture you loaded, so if a run
behaves like a different posture than you expected, check what you copied.

Use `mpremote`, not `ampy`: on a board with native USB (the T3S3 and anything else ESP32-S3)
`ampy` hangs on the REPL rather than copying.

Then open a serial monitor on each (`screen /dev/cu.usbmodemXXXX 115200`, or `picocom -b 115200 …`)
and reset the board. Each prints its type and MAC on boot; the Hub then pulls the file and
prints or saves it under `Results/<label>/`.

## The one thing every posture needs

**RF config must match** on both `LoRa.json` files: `sf` / `freq` / `bandwidth` / `coding_rate`.
They are identical in all three folders here (SF7 / 868 / 125 / 4-5). Changing those two files is
enough: the Hub's endpoint states no radio of its own, so it is polled on whatever the Hub's
`LoRa.json` says.

Each posture then has its own second thing to line up, which is what its README is about.

## If the radio link needs debugging

Set `"debug": true` inside the `connector` block (very verbose: it shows every send and recv).
Start with SF7 (fast); at SF11/12 bump `max_timeout`. If nothing arrives at all, double-check the
two boards are on the same `freq`/`sf` and that antennas are attached.

`capture_serial.py` here logs both serial ports to file, which is what the `.log` files beside it
came from. It is a bench instrument rather than part of any example.

# pro: the same nodes, with the board's screen, card and LED switched on

This is not a fourth posture. It is the [`open/`](../open) pair with one extra block in each
config, and that block drops into `secure/` and `control/` unchanged. The postures are about who
the peer is; this is about what the board in your hand does while it talks.

There is no `main_pro.py`. There used to be three of them, one per board, each carrying a whole
node program around the two features it existed to add, and each one still calling a v2 handshake
that a v3 node refuses. They are gone. The program here is the same
[`main.py`](../main.py) every other node runs.

## The `device` block

```json
"device": {
  "board": "t3s3",
  "log_file": "/sd/logs/log.log"
}
```

`board` is the only required key. It names the board file the program builds, from the table in
`build_board` in `main.py`, next to the radio table and there for the same reason: a board is a
pin map and a list of what is soldered on, which belongs to the deployment rather than to the
protocol. Adding a board is one import and one branch in a file you own.

Everything else is optional:

| key | default | what it does |
|---|---|---|
| `screen` | whatever the board has | draws MAC, RSSI, SNR, SF, bandwidth, TX power and chunk count |
| `sd` | whatever the board has | mounts the card, so `queue_path` can point at it |
| `led` | whatever the board has | blinks a heartbeat on its own thread |
| `sd_mount_point` | the board's own (`/sd`) | where the card is mounted |
| `log_file` | off | writes the live values to a file, one JSON object per line |
| `logo_file` | `AlLoRa_logo.json` | the 32x32 logo in the left of the display |
| `screen_button` | on | BOOT toggles the display off and on |

**Say nothing and you get what the board has.** A T3S3 has a screen, a card slot and an LED, so a
config naming only `board` lights all three. **Ask for something the board does not have and the
node stops** and says so, rather than running on and looking healthy: a config asking for a screen
on a screenless board was written for a different board, and the place to find that out is at
boot, not at the antenna.

To leave one off, say so:

```json
"device": { "board": "t3s3", "screen": false }
```

## Which failures are fatal

**A card that will not mount stops the node only if the files it moves live on the card.** The
Edge here serves `/sd/Outbox` and the Hub writes `/sd/Results`, so for both of them a dead card
means a node that runs, polls, and moves nothing: it stops instead, naming the path. Point
`queue_path` at internal flash and the same dead card is only a missing log, so it says so and
carries on.

**A missing logo is never fatal.** `AlLoRa_logo.json` is a file on the board, not part of the
program. Without it you get the readings and no logo.

## Load it

Beyond the two files every posture needs, a pro node wants the logo and a card with the folders
on it:

```bash
# Edge
mpremote connect /dev/cu.usbmodemXXXX fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp pro/edge/AlLoRa.json :AlLoRa.json
mpremote connect /dev/cu.usbmodemXXXX fs cp AlLoRa_logo.json :AlLoRa_logo.json

# Hub
mpremote connect /dev/cu.usbmodemYYYY fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemYYYY fs cp pro/hub/AlLoRa.json :AlLoRa.json
mpremote connect /dev/cu.usbmodemYYYY fs cp pro/hub/Nodes.json :Nodes.json
mpremote connect /dev/cu.usbmodemYYYY fs cp AlLoRa_logo.json :AlLoRa_logo.json
```

Put the card in, with an `Outbox` folder on the Edge's card holding whatever you want to send.
The node creates the folder itself on first boot if it is not there; it cannot create the card.

## What the board layer is, and where it lives

Everything that knows a pin, a bus or a chip lives under
[`firmware/targets/t3s3-sx127x/modules/`](../../../firmware/targets/t3s3-sx127x/modules): the
board file `lora32.py`, and `board/` beside it holding the screen, the card and the LED. That
directory used to be called `utils/`, which collided with `AlLoRa/utils/` and blurred the one
line worth keeping sharp. `board/` is where a pin is allowed to appear.

The log is the exception, and it proves the rule: it names no pin, so it moved the other way,
into the library as `AlLoRa/Subscribers/Logger.py`, and it ships on every board including the
ones with nothing soldered to them.

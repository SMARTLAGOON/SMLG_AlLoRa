# v3 hello-world: two T3S3, a file over LoRa

The smallest end-to-end v3 test: one **Edge** serves a 1000-byte file, one **Hub** pulls it. No
`establish_connection()`, just `send_file` / `listen_to_endpoint` running the whole flow, exactly
like the loopback acceptance tests.

It comes in three postures. They are the same transfer with more provisioning underneath it, and
each one is a folder of config files you copy onto two boards beside the one shared program.
Start at the top and stop when you have what you need.

| Folder | What it adds | The Hub registers the Edge by |
|---|---|---|
| [`open/`](open) | Nothing. No identity, no keys, frames in the clear. | a `session_id` you assign to both |
| [`secure/`](secure) | A crypto identity per node: ECDH on first contact, every frame AEAD-sealed. | the `device_id` the Edge derives |
| [`control/`](control) | A fleet control root: the Hub signs configuration commands, the Edge verifies them. | the same `device_id`, plus a shared root |

Beside them, [`pro/`](pro) is not a fourth posture: it is the `open/` pair with the board's
screen, card and LED switched on through a `device` block, and that block drops into any of the
three unchanged.

## One program, six configs

[`main.py`](main.py) here is the program every node runs, whichever posture and whichever
placement. It reads `AlLoRa.json`, builds what that file names, and runs it, so an Edge and a Hub
differ by one line of JSON rather than by two Python files. It is also the program the wizard
pushes, so a deployment you set up by hand and one `allora setup` produces are the same
deployment.

There was previously one `main.py` per side per posture, six near-copies, each naming its radio
in its first import. That is what made an operator's radio choice unreachable: asking for an
SX1262 Edge produced an SX127x one, because the program said so and nobody reads the program.

[`secure/edge/main_literal.py`](secure/edge/main_literal.py) is the same deployment written out
longhand, every class named. Read it if you would rather see the classes than follow a dispatch,
and start from it if you have a radio this repository has never supported.

## Where files come from, and where they go

A node's two boundaries are `DataSource` in and `DataSink` out, and the config names them the
same way it names a radio. Both keys are optional, and leaving them out gives exactly the node
you had before they existed: an Edge serves the folder at `queue_path`, and a Hub writes what
arrives under `result_path`.

```json
"data_sink":  { "kind": "mqtt", "host": "10.0.0.4", "topic_prefix": "albufera" },
"datasource": { "kind": "mqtt", "topics": ["sensors/#"] }
```

A sink's `kind` is `disk`, `mqtt` or `http`; a source's is `disk` or `mqtt`. Every other key
inside the block is passed straight to that class, so what you leave out is whatever the class
already defaults to, and there is one place to read it. A key the kind does not take stops the
boot rather than being ignored, which is what makes a typed `hosts` a halt instead of a node
quietly publishing to localhost.

The `http` sink posts each finished file to a web service, which is how a deployment reaches a
control website:

```json
"data_sink": { "kind": "http", "url": "https://control.example/api/ingest", "token": "..." }
```

The file bytes are the request body and the transfer's own record (source, session, device id,
RSSI, SNR, chunk count) travels in `X-AlLoRa-*` headers. The Hub always posts outward, because a
Hub usually sits behind NAT and is asleep half the time, so nothing can reach in to collect from
it. If the service refuses, the file is not thrown away: the transfer goes unacknowledged and the
next round pulls it again.

`url` is the one key in any block with no default, so an `http` block without one stops the boot.
`token` is worth a thought before you write it: it lands in a config file on the board's
filesystem, so treat it as a per-Hub credential you can revoke, not a shared secret. On a board
whose `urequests` build takes no `timeout`, set `"timeout": null`.

Two rules worth knowing before you write one:

- **A Hub serves a different downlink to each Edge**, so its `datasource` block goes in that
  Edge's entry in `Nodes.json`, not at the top of the Hub's own config. A disk block there
  carries its own `queue_path`, because there is no outer key for it to fall back on.
- **A node holding a control root cannot name a `data_sink`.** Its sink slot is the verify gate
  that checks signatures on incoming commands, and a config key able to take that slot would
  disarm the signature check with a text edit. Such a config is refused at boot.

Passing a boundary to the constructor still works and still wins:
[`../Hubs/Many-Edges/USB/main_mqtt.py`](../Hubs/Many-Edges/USB/main_mqtt.py) is that shape, and
it is the way in for a boundary this repository has never heard of.

## Flash and load

All three postures run the same AlLoRa firmware on both boards. Then put one posture's files on
its boards, picking the folder from the table above:

```bash
# Edge device
mpremote connect /dev/cu.usbmodemXXXX fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp open/edge/AlLoRa.json :AlLoRa.json
# something to send: an Edge serves the files in its outbound folder and waits when it has none
mpremote connect /dev/cu.usbmodemXXXX fs mkdir Outbox
mpremote connect /dev/cu.usbmodemXXXX fs cp some-file.bin :Outbox/some-file.bin

# Hub device
mpremote connect /dev/cu.usbmodemYYYY fs cp main.py :main.py
mpremote connect /dev/cu.usbmodemYYYY fs cp open/hub/AlLoRa.json :AlLoRa.json
mpremote connect /dev/cu.usbmodemYYYY fs cp open/hub/Nodes.json :Nodes.json
```

The same `main.py` goes on both boards. Which one becomes the Edge and which the Hub is decided
by the `"node"` line in the `AlLoRa.json` beside it, and which radio each drives is decided by
`connector.driver` in the same file.

The Hub polls the Edges listed in `Nodes.json`, which is the file it also writes settled radio
settings back into. In `open/` that roster is ready to run; in `secure/` and `control/` you paste
in the `device_id` the Edge prints on boot and set `"active": true`.

Nothing on the board records which posture you loaded, so if a run behaves like a different
posture than you expected, check what you copied.

**A board that has never been updated keeps working.** A node reads `AlLoRa.json` if it is there
and `LoRa.json` if it is not, and writes back to whichever one it read. The old name is read
forever, not for a migration window.

Use `mpremote`, not `ampy`: on a board with native USB (the T3S3 and anything else ESP32-S3)
`ampy` hangs on the REPL rather than copying.

Then open a serial monitor on each (`screen /dev/cu.usbmodemXXXX 115200`, or `picocom -b 115200 …`)
and reset the board. Each prints its type and MAC on boot; the Hub then pulls the file and
prints or saves it under `Results/<label>/`.

## The one thing every posture needs

**RF config must match** on both `AlLoRa.json` files: `sf` / `freq` / `bandwidth` /
`coding_rate`. They are identical in all three folders here (SF7 / 868 / 125 / 4-5). Changing
those two files is enough: the Hub's endpoint states no radio of its own, so it is polled on
whatever the Hub's own config says.

**`chunk_size` is absent on purpose.** Left out, a node cuts its files at the largest chunk its
frames can carry, which depends on the posture and the protocol version and so is only knowable
on the node. Set it to pin a number; it is still clamped to what actually fits.

Each posture then has its own second thing to line up, which is what its README is about.

## If the radio link needs debugging

Set `"debug": true` inside the `connector` block (very verbose: it shows every send and recv).
Start with SF7 (fast); at SF11/12 bump `max_timeout`. If nothing arrives at all, double-check the
two boards are on the same `freq`/`sf` and that antennas are attached.

`capture_serial.py` here logs both serial ports to file, which is what the `.log` files beside it
came from. It is a bench instrument rather than part of any example.

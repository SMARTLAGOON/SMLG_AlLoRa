# v3 hello-world: two T3S3, a file over LoRa

The smallest end-to-end v3 test: one **Edge** serves a 1000-byte file, one **Hub** pulls it. No
`establish_connection()`, just `send_file` / `listen_to_endpoint` running the whole flow, exactly
like the loopback acceptance tests.

## Flash + load

Both boards run the same AlLoRa firmware. Then put each node's files on its board:

```bash
# Edge device
mpremote connect /dev/cu.usbmodemXXXX fs cp edge/main.py :main.py
mpremote connect /dev/cu.usbmodemXXXX fs cp edge/LoRa.json :LoRa.json

# Hub device
mpremote connect /dev/cu.usbmodemYYYY fs cp hub/main.py :main.py
mpremote connect /dev/cu.usbmodemYYYY fs cp hub/LoRa.json :LoRa.json
```

Use `mpremote`, not `ampy`: on a board with native USB (the T3S3 and anything else ESP32-S3)
`ampy` hangs on the REPL rather than copying.

Then open a serial monitor on each (`screen /dev/cu.usbmodemXXXX 115200`, or `picocom -b 115200 …`)
and reset the board. Each prints its type + MAC on boot; the Hub then pulls the file and
prints/saves it under `Results/<label>/`.

## The two things that must line up

1. **RF config must match** on both `LoRa.json`: `sf` / `freq` / `bandwidth` / `coding_rate`.
   (They're identical here: SF7 / 868 / 125 / 4-5.) Changing those two files is enough: the Hub's
   endpoint states no radio of its own, so it is polled on whatever the Hub's `LoRa.json` says.
2. **`session_id` must match**, because v3 addresses by session id, not MAC. Both are `42` here.
   The Hub's `mac_address` field is just a label and the save-folder name; it doesn't affect
   addressing in v3 (set it to the Edge's printed MAC if you want a meaningful folder).

## Go secure

Two changes (not just the config flip):

1. **Both** `LoRa.json`: `"security_mode": "open"` becomes `"secure"` (use the ready-made
   `LoRa_secure.json` in each folder). The Edge's carries `identity_file`, so its crypto
   identity (and thus its `device_id`) is stable across reboots.
2. **`hub/main.py`**: register the Edge by its **`device_id`**, not a MAC. On boot the
   Edge prints `EDGE device_id (register this on the Hub): <hex>`. Paste that value:
   ```python
   endpoint = Digital_Endpoint(name="src", device_id="<the printed device_id>", active=True)
   ```
   First contact is addressed by `device_id[:4]` (no MAC on the wire), and the session id derives
   from the same identity on both ends, so there is **no `session_id` to keep in sync**. That
   `device_id[:4]` is also what names the Hub's save folder for an identity-registered Edge.

Then the ECDH handshake runs automatically on first contact (needs the CTR-flag firmware, which
the CI build has) and every frame is AEAD-sealed. Watch the Edge's serial: instead of
`Could not parse frame`, you'll see it answer the handshake CTRL frames, then the sealed transfer.

## Control: let the Hub retune the Edge over the air

Changing the radio settings above means editing two files and rebooting two boards. A Hub can
also do it over the air with `hub.ask_change_rf(endpoint, {"sf": 9, "trial": 300})`. How that
command is authenticated depends on what the nodes were provisioned with, and provisioning is
the whole of the setup:

* **Nothing provisioned** (the plain example above): the command travels in band on the link
  itself, unsigned. Fine for a survey run, where the link authenticates nothing anyway.
* **A control root provisioned**: the command is a signed artifact, and the node then refuses
  unsigned ones. That refusal is the point of provisioning: if an unsigned frame still worked,
  the signature would be protecting nothing.

Signed control needs `"security_mode": "secure"` on both nodes, because an artifact names the
node it is for by its `device_id`, and an open node has none.

### Running it

`edge/main.py` already carries the receiving half, and picks its side from the config: with no
control root it accepts commands on the link itself, with one it accepts only what that root
signed. Nothing to change there. On the Hub board, load `hub/main_control.py` as `main.py`
instead of `hub/main.py`:

```bash
mpremote connect /dev/cu.usbmodemYYYY fs cp hub/main_control.py :main.py
```

It pulls one file first so the link is known good, commands the retune, and keeps polling.
Watch the Edge's serial for `Changing RF Config to:` and then, once a whole file has crossed on
the new settings, `RF trial committed`. The Hub prints which transport it chose on boot.

If the new configuration cannot carry a transfer, nobody has to intervene: the Edge holds it
only for `trial` seconds, rolls back to the last configuration that worked, and the Hub's
`{new, old}` probe finds it there. That undo is the reason a retune is safe to try at all.

`ask_change_rf` answers with one of four words, and each one is a different next move:

| It says | It means | What to do |
|---|---|---|
| `accepted` | The Edge acknowledged the command and both ends moved. | Nothing. |
| `pending` | A signed artifact is queued. It is handed over on one of the pulls, and the Edge decides afterwards, on its own. | Keep polling and read `hub.rf_change_status(endpoint)`, which the probe settles a few visits later. |
| `refused` | The Edge answered a poll, so it is there and listening, and it declined the command. | Look at provisioning, not at the radio. An unsigned command is refused by a node that holds a control root. |
| `unreachable` | Nothing answered, command or poll. | Look at the link. The command was never considered. |

Telling `refused` from `unreachable` is why the Hub asks one extra short question after a
command goes unanswered, and only then. A refusal and a broken antenna produce exactly the same
silence, and reporting them as one thing sent people to check hardware when the repair was a
missing key.

Create the fleet's root once, on your laptop:

```bash
python3 provision_control_root.py
```

That writes `hub/control_root.key` (the signing half, 64 hex characters) and
`edge/control_root.key` (the verifying half, 130). Same filename on both, like `identity.key`:
what the file *contains* is what decides whether a node signs commands or checks them. Running
it again never replaces an existing root, so adding a node later is safe.

Copy each half to its own board and add one line to **both** `LoRa_secure.json`:

```bash
mpremote connect /dev/cu.usbmodemHUB  fs cp hub/control_root.key  :control_root.key
mpremote connect /dev/cu.usbmodemEDGE fs cp edge/control_root.key :control_root.key
```

```json
  "control_root_file": "control_root.key",
```

That one line is the whole provisioning. Both nodes then keep a counter file beside the key
without being told to, in `control.counter`: the Hub remembers how many commands it has issued
under this root, and the Edge remembers the highest one it has accepted. That is what stops a
command being recorded off the air and replayed back later, and it is why a reboot on either
end does not leave the pair unable to talk. Set `control_counter_file` if you want the file
somewhere else.

Three things that will bite otherwise:

1. **The private half never goes on an Edge.** A node handed it refuses to boot and says so,
   because that key signs for the whole fleet and it has no business on a node in the field.
2. **A named file that is missing is a startup error, not a warning.** Unlike `identity.key`,
   a control root is never generated on the board: a node that invented its own would be its
   own authority. If the config names one, the node halts until it is there.
3. **Keep the root.** Every node is pinned to it. Replacing it means re-provisioning every
   board by hand, which is also the deliberate way to reset a fleet's command counters.

## If the radio link needs debugging

Set `"debug": true` inside the `connector` block (very verbose: it shows every send/recv). Start
with SF7 (fast); at SF11/12 bump `max_timeout`. If nothing arrives at all, double-check the two
boards are on the same `freq`/`sf` and that antennas are attached.

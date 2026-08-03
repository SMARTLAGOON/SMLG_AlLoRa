# v3 hello-world: two T3S3, a file over LoRa

The smallest end-to-end v3 test: one **Edge** serves a 1000-byte file, one **Hub** pulls it. No
`establish_connection()`, just `send_file` / `listen_to_endpoint` running the whole flow, exactly
like the loopback acceptance tests.

## Flash + load

Both boards run the same AlLoRa firmware. Then put each node's files on its board:

```bash
# Edge device
ampy -p /dev/cu.usbmodemXXXX put edge/main.py main.py
ampy -p /dev/cu.usbmodemXXXX put edge/LoRa.json LoRa.json

# Hub device
ampy -p /dev/cu.usbmodemYYYY put hub/main.py main.py
ampy -p /dev/cu.usbmodemYYYY put hub/LoRa.json LoRa.json
```

Then open a serial monitor on each (`screen /dev/cu.usbmodemXXXX 115200`, or `picocom -b 115200 …`)
and reset the board. Each prints its type + MAC on boot; the Hub then pulls the file and
prints/saves it under `Results/<label>/`.

## The two things that must line up

1. **RF config must match** on both `LoRa.json`: `sf` / `freq` / `bandwidth` / `coding_rate`.
   (They're identical here: SF7 / 868 / 125 / 4-5.)
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

## If the radio link needs debugging

Set `"debug": true` inside the `connector` block (very verbose: it shows every send/recv). Start
with SF7 (fast); at SF11/12 bump `max_timeout`. If nothing arrives at all, double-check the two
boards are on the same `freq`/`sf` and that antennas are attached.

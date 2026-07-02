# v3 hello-world — two T3S3, a file over LoRa

The smallest end-to-end v3 test: one **Source** serves a 1000-byte file, one **Collector**
(Requester) pulls it. No `establish_connection()` — `send_file` / `listen_to_endpoint` run the
whole flow, exactly like the loopback acceptance tests.

## Flash + load

Both boards run the same AlLoRa firmware. Then put the role's files on each:

```bash
# Source device
ampy -p /dev/cu.usbmodemXXXX put source/main.py main.py
ampy -p /dev/cu.usbmodemXXXX put source/LoRa.json LoRa.json

# Collector device
ampy -p /dev/cu.usbmodemYYYY put collector/main.py main.py
ampy -p /dev/cu.usbmodemYYYY put collector/LoRa.json LoRa.json
```

Then open a serial monitor on each (`screen /dev/cu.usbmodemXXXX 115200`, or `picocom -b 115200 …`)
and reset the board. Each prints its role + MAC on boot; the Collector then pulls the file and
prints/saves it under `Results/<mac>/`.

## The two things that must line up

1. **RF config must match** on both `LoRa.json` — `sf` / `freq` / `bandwidth` / `coding_rate`.
   (They're identical here: SF7 / 868 / 125 / 4-5.)
2. **`session_id` must match** — v3 addresses by session id, not MAC. Both are `42` here. The
   Collector's `mac_address` field is just a label + the save-folder name; it doesn't affect
   addressing in v3 (set it to the Source's printed MAC if you want a meaningful folder).

## Go secure

Flip **one field in both** `LoRa.json`: `"security_mode": "open"` → `"secure"`. Nothing else
changes — the ECDH handshake runs automatically on first contact (needs the CTR-flag firmware,
which the CI build has), then every frame is AEAD-sealed. Watch the Source's serial: you'll see
the handshake CTRL exchange before the transfer.

## If the radio link needs debugging

Set `"debug": true` inside the `connector` block (very verbose — shows every send/recv). Start
with SF7 (fast); at SF11/12 bump `max_timeout`. If nothing arrives at all, double-check the two
boards are on the same `freq`/`sf` and that antennas are attached.

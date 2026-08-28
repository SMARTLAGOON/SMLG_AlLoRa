# AlLoRa firmware — freeze the protocol into MicroPython, per target

A **target = one (device × radio modem)**. Building a target freezes `AlLoRa/` (the protocol,
from the repo root) plus that target's **chip driver** and **board helpers** into a custom
MicroPython firmware, so `import AlLoRa` just works on the device with no filesystem copy.

The protocol is already modem-agnostic — every radio is a `Connector` (`SX127x_connector`,
`SX1262_connector`, `E5_connector`, …) behind one interface, so the engine never knows the
chip. The only things that vary per hardware are the **low-level driver** and the **board
config**, and this layout keeps each in exactly one place:

```
firmware/
  drivers/                 # chip drivers, vendored + pinned WITH the protocol (varies by MODEM)
    PyLora_SX127x_extensions/    SX127x / SX1276  (upstream: github.com/GRCDEV/PyLora_SX127x_extensions)
  targets/                 # one folder per (device, modem)
    t3s3-sx127x/           #   the primary board (LilyGo T3S3 + SX127x)
      boards/<BOARD>/      #     MicroPython board overlay: sdkconfig.board, mpconfigboard.*, manifest.py
      modules/             #     board helpers to freeze (lora32, lilygo_oled, utils)
  patches/                 # version-specific MicroPython source patches (see patches/README.md)
  T3S3/                    # (legacy) flashing instructions + an old prebuilt .bin
```

## Building

CI does it — a GitHub Actions workflow (`.github/workflows/build-firmware.yml`) builds every
target in a matrix inside Espressif's ESP-IDF container and attaches each `firmware.bin` to the
release. No local toolchain. Each matrix row pins the MicroPython + ESP-IDF versions for that
target (they're coupled — a MicroPython release supports specific ESP-IDF versions).

The freeze copies the **live** `AlLoRa/` tree at build time — including `AlLoRa/Links/` (the
`Serial_link` / `WiFi_link` tunnel transports). A device that runs tunnel code (the Adapter
bridge, whose `Adapter` moves frames over a real UART or WiFi socket) therefore only
picks up a tunnel change on the next rebuild; a protocol-only push does not reach it. The current
freeze additionally carries the `DataSources`/`DataSinks` application boundaries, Hub-commanded role
reversal, and the two-ended RF_CONFIG coordination, so the `Nodes` and config-actuation paths
likewise only reach the device on a rebuild. It also carries the `backup_config` fix that makes a
committed RF trial actually survive a reboot (the earlier freeze wrote the live RF back under keys
the loader never read, so a committed sf7→sf9 came back as sf7 on the next boot); reboot-persistence
therefore only works once the device runs a rebuild from this freeze.

The structure pass changes the **set** of frozen modules, not just their contents:
`AlLoRa/Nodes/Swap_base.py` is gone and its two whole-file loops now live on `AlLoRa/Nodes/Node.py`,
so a device flashed before it holds a module the source no longer has. It also changes how a
`device_id`-registered endpoint is named off the air (results folder, MQTT topic, status line), which
a Hub polling several registered Edges needs. Both reach a board only on a rebuild, and this
paragraph is the trigger for one: the workflow's push filter watches `firmware/**`, so a
protocol-only commit never starts a build on its own.

This freeze is the first to carry **control**. `AlLoRa/Control/` gains two modules a board flashed
before it simply does not have: `Control_Root.py`, which mints a command, and
`control_envelope.py`, the signed wrapper and the counter that lets one expire. `Security/ec_p256.py`
gains signing, where it could previously only verify. Both routes a command can travel now exist on
the device: a signed artifact, delivered through `Control_Root_DataSink` and refused unless the
provisioned root vouches for it, and an unsigned command carried on the link itself for an open pair
with no root to check against. The RF trial is bounded here too, so an Edge moved onto settings it
cannot be heard on puts the old ones back by itself rather than going deaf, and the Hub now treats
the visit after an in-band command as a real probe, without which a retune that worked is thrown
away and the two ends drift onto different configurations. None of that reaches a board any way but
a rebuild, and this paragraph is the trigger for one.

The freeze before this one carried the control paths but could not finish a signed command on a
board: the verify gate fingerprinted the control root with `hexdigest()`, which MicroPython's
hashlib does not have. Anything reaching past `digest()` passes the whole CPython suite and
throws on every device, which is worth remembering when adding a target. A board flashed from
that build actuates a signed command, then aborts the reception, so the commanding end never
mirrors the change and the two finish on different radio configurations. It also leaves the
replay counter as an empty file that reads back as zero on the next boot. Reflash any board
carrying it.

## Adding a target (new device or new modem)

1. If it's a **new modem**, vendor its driver under `drivers/<name>/` (pin the exact version).
2. Add `targets/<device>-<modem>/` with `boards/<BOARD>/` (the board overlay) and `modules/`
   (board helpers). For **secure** mode, enable native AES-CTR in `mpconfigboard.h` and
   `require("hmac")` in `manifest.py`. The CTR flag is `MICROPY_PY_CRYPTOLIB_CTR` on MicroPython
   ≥ v1.21 (renamed from `MICROPY_PY_UCRYPTOLIB_CTR`); define both to be version-safe. Get it
   wrong and `detect_aead()` returns None → the node silently degrades to open.
3. Add a row to the workflow matrix (target dir, board, driver, MicroPython/ESP-IDF versions).

That's the whole "which MicroPython, which modem, which device" decision — one folder + one
matrix row. A supplier switching modems is a new driver + a new target; the protocol is untouched.

## Secure mode: MicroPython vs CPython (why CI-green ≠ device-works)

The test suite runs on CPython, whose stdlib is a superset of MicroPython's. Several CPython-only
APIs pass CI but throw on the ESP32, and because the secure node **degrades to open** when its
crypto backend is unavailable, the failure is silent on the wire — it just never completes the
handshake. Known traps the frozen library must avoid (all fixed, listed so they stay fixed):

- **`import ucryptolib`** — renamed to `cryptolib` in MicroPython v1.21 with no weak-link alias.
  Import `cryptolib` first, fall back to `ucryptolib` for pre-1.21. (`AlLoRa/Security/AEAD.py`)
- **`hmac.compare_digest`** — CPython-only; micropython-lib's `hmac` has only `HMAC`/`new`. Use a
  local constant-time compare instead. (`AlLoRa/Security/AEAD.py`)
- **`hashlib.sha256().digest_size`** — MicroPython hash objects don't expose it. Hardcode `32`.
  (`AlLoRa/Security/kdf.py`)
- **`int.bit_length()`** — not available on MicroPython. The P-256 scalar-mult ladders reduce the
  scalar mod N and then read a fixed 32 bytes of it, so they never need to ask how long it is.
  Reading it as bytes also keeps every digit a small integer rather than allocating a fresh
  256-bit one per step. (`AlLoRa/Security/ec_p256.py`)

- **`del buf[:n]`** — slice deletion exists on CPython and not on MicroPython, which raises
  `TypeError: 'bytearray' object doesn't support item deletion`. Rebuild the tail instead
  (`buf = self._buf = bytearray(buf[n:])`). Deleting a dict *key* is fine and is used on-device;
  only the slice form is missing. This one sat in the tunnel's frame reader rather than the
  secure path, so the whole list above looked green while the first frame ever to reach a bridge
  board raised. (`AlLoRa/Links/Serial_link.py`)

`tests/test_micropython_portability.py` scans the frozen secure path for these names so CI, not
the ESP32, is what fails when a new one creeps in. It also walks the **whole** frozen tree for
slice deletion, which no curated secure-path list would have covered: a bridge board freezes and
runs the link and adapter layers too, and those had never run on hardware until the USB rig.

Rule of thumb: after any change to the secure path, don't trust CI alone: flash and confirm the
Edge boots **without** the `secure mode … running open (degraded): …` line, whose suffix now
names the exact backend failure. Note the frozen library is baked into the `.bin`, so a
library-only change needs a firmware rebuild to reach the device.

**Registering a secure node (device_id, not MAC).** A secure node's first contact is addressed by
its device_id, the fingerprint of its long-term identity key (`SHA256(pubkey)`), not by its wifi
MAC. On first boot the Edge generates that key, persists it to the `identity_file` named in
`AlLoRa.json` (so the device_id is stable across reboots), and prints it as `EDGE device_id
(register this on the Hub): <hex>`. Bring-up is therefore two passes: boot the Edge once to
read its device_id, then register that value on the Hub (`Digital_Endpoint(device_id="<hex>",
active=True)`) before starting the pull. The session id derives from the same identity, so no
hand-assigned `session_id` is needed; set one in config only to override for debugging or to break a
rare 1-byte clash. A node registered by MAC instead keeps the legacy two-MAC handshake.

**Handshake CPU budget.** First contact runs pure-Python P-256 ECDH — a few scalar multiplications
per session, on the order of half a second each on an ESP32-class board (they compute in Jacobian
coordinates; the earlier affine version took ~13 s and overran the handshake receive window, so the
peer looped `Handshake failed`). This is a one-off per-session cost, never per frame, but it does
block the radio loop while it runs, so keep the handshake receive window comfortably larger than a
single scalar multiplication.

**Session recovery after a peer reboot.** A session lives in RAM, so an endpoint that reboots comes
back with none, and only the authority can offer a new one. The authority now notices by itself: a
run of silent visits re-arms the connection poll (the one request an endpoint always replies to),
and an endpoint that stays silent through that poll has its session dropped so the next visit
re-handshakes. Silence alone is deliberately not enough, since an endpoint with no file answers a
metadata poll with nothing at all and would otherwise be re-keyed while merely idle. The budget is
`session_recovery_after` (default 3 visits) on the Hub ctor. This matters most for the RESET control
artifact, which reboots an endpoint on purpose: rebuild to reach it.

**The bridge no longer pauses between back-to-back verbs.** The adapter loop's 100 ms pause now
fires only when there was nothing to serve, or when the link raised. Any tunnel timing measured
against a `.bin` built before this is not comparable: the old build spent 100 ms per transport
verb inside the bridge, roughly 15% of a 1 KB transfer. Re-measure rather than diffing against a
recorded run.

**Validating endpoint radio defaults needs a non-SF7 bench.** An endpoint that states no radio
settings of its own now follows the node that polls it, resolved once when the endpoint is
constructed rather than read live (mid-round the radio sits on whichever endpoint was visited last,
so a live read would make every unstated endpoint inherit its neighbour). The previous behaviour was
a hardcoded SF7, so at SF7 the old and new code are indistinguishable and a green run proves
nothing. Flash a Hub whose own config is some other spreading factor, register an endpoint with no
`connector` block, and confirm the transfer completes at the Hub's factor.

**A refused control command no longer reads as a dead link.** Asking an endpoint to change its
radio now answers `accepted`, `refused`, `pending` or `unreachable` rather than true or false, and
`rf_change_status` reads the verdict that a signed change cannot have yet at the moment it is
queued. Both halves live in the frozen library, so a board flashed from an older `.bin` still
reports a refusal as silence and still reports a signed refusal as success: reflash both ends
before testing any of it. The wire is untouched, so a new build and an old one interoperate.

**A node now writes a durable file whole, or not at all.** A Hub remembers where it moved an
endpoint by writing the settled radio settings back into `Nodes.json`, and the files a node cannot
afford to lose (its own config backup, the two counter files, and the identity key) are written to a
temporary file and renamed into place rather than rewritten where they sit, so an interrupted write
leaves the previous file intact instead of half a file. Two parts of this only show on a board.
Whether `os.rename` can replace an existing file depends on how the flash was formatted, since
littlefs replaces the target and FAT refuses, so the write retries with a remove first; and if that
fallback is the path a board takes, there is a brief moment where `Nodes.json` is absent and
`Nodes.json.tmp` holds the whole file, which nothing recovers on boot. All of it lives in the frozen
library, so a board flashed from an older `.bin` still rewrites each of those files in place and
still forgets an endpoint it retuned: reflash both ends before testing any of it.

**A node can now queue readings on flash, and a replay mark survives a restart.** Three changes
that only reach a device on a rebuild:

- **The outbound queue can live in a folder instead of in RAM.** `Disk_DataSource` writes each
  reading to flash and serves the backlog in order, so a node that takes measurements while the link
  is down still has them after a reboot. Serving now borrows the head of the queue and only drops it
  once the far end confirms, which means an interrupted transfer is retried rather than lost, and a
  lost acknowledgement produces a duplicate. On a board this is the part worth testing: pull power
  mid-transfer and confirm the backlog comes back in order. Whether it holds depends on the flash
  format for the same reason the durable config writes do, so run it on the card the deployment will
  actually use.
- **A node that verifies signed commands now keeps its replay mark in a file by default.** It used
  to sit in RAM unless whoever built the node named a file for it, so a signed command recorded off
  the air could be replayed after a restart and accepted a second time. Both node kinds now read
  where that file goes from the same config line. A board flashed from an older `.bin` still forgets
  the mark on every reboot: reflash both ends before trusting any replay test.
- **`short_mac` is gone from the v3 configs.** It selected a long-MAC option v3 never had, and no v3
  path read it. Copy the current `AlLoRa.json` onto the device rather than keeping an older one;
  the key was inert either way, so nothing changes on the air.

The wire is untouched by all three, so a new build and an old one still interoperate.

**A node that resets mid-transfer now re-announces its file instead of splicing it into the last
one.** This only reaches a device on a rebuild, and both ends need the new `.bin`.

A node that reset while serving a file used to answer the collector's next chunk request out of
whatever file it queued next. Nothing caught it: every frame was intact and no chunk index was
missing, so the collector saved one file whose bytes came from two different files and reported
success. A node now answers a chunk request with METADATA whenever it cannot remember announcing
the file it is holding, which is exactly what a reset destroys, and the collector reads that reply
as a re-opened transfer and pulls the file from the beginning.

On a board this is the part worth testing, and it takes two runs. Reset a node serving from the
RAM queue mid-transfer and confirm the collector reports asking for metadata rather than re-asking
the same chunk, that nothing lands under the interrupted file's name, and that the replacement
arrives byte-correct under its own. Then run the same reset with `Disk_DataSource` and confirm the
transfer still picks up where it left off: a source whose files survive a restart is the one case
allowed to resume, and that is the behaviour the first run must not have broken.

A collector on an older build re-asks the same chunk index and reads the METADATA as no reply, so
it stalls until its visit expires instead of saving a spliced file. A wasted visit rather than a
corrupt one.

**A board can now be a modem over its own USB socket, and a bridge no longer prints onto the
wire.** Both only reach a device on a rebuild, and the USB tunnel needs the new `.bin` before
its example will even import.

`AlLoRa/Adapters/USB_adapter.py` is a module a board flashed before this freeze simply does not
have. It is the Adapter for a board reached over native USB, where there is no `machine.UART`
behind the socket and the console itself is the link. `Serial_link` gains the port that wraps
that console, `bridge_usb()`, and the host end is unchanged: a CDC board is an ordinary serial
device, so `Serial_connector` opens it the same way it opens a GPIO tunnel.

Two fixes ride along in shared code, so they reach the pin rig too. The frame reader now drops
whatever the medium injected ahead of a frame, because the sentinel says where a frame ends and
never where it starts, and boot chatter is printable enough to survive the text filter and reach
the JSON parser. And the library's debug output can be moved off stdout in one call, which is
what keeps a bridge's own logging, the radio Connector's most of all, from landing inside a
frame. On a UART both were rare; on a console that is also the wire the first happens on every
reset.

On a board this is the part worth testing, and the runsheet is the cockpit's. Confirm a transfer
completes over one USB cable with no jumpers, that `preamble_dropped` stops climbing once the
session is up rather than growing with every frame, and that resetting the bridge mid-session
costs one retry rather than the session. Nothing may be attached to the bridge board's console
while any of that runs: an `mpremote` or a `screen` on that port eats the bytes the host is
waiting for.

# `tools/` — the provisioning wizard

Host-side code. CPython only, never frozen into firmware: the manifest freezes `AlLoRa/`, so
nothing here reaches a board.

```bash
python3 tools/provision.py fleet-init
python3 tools/provision.py edge --firmware AlLoRa-t3s3-sx127x-firmware.bin
python3 tools/provision.py hub  --firmware AlLoRa-t3s3-sx127x-firmware.bin
python3 tools/provision.py verify --edge-port /dev/cu.usbmodemAAAA \
                                  --hub-port  /dev/cu.usbmodemBBBB
```

Python 3.8 or newer, and nothing installed: the wizard is stdlib plus AlLoRa's own pure-Python
crypto.

It does need `mpremote` and `esptool` on PATH. They are shelled out to, not imported, and they
are deliberately not part of the AlLoRa install. Start with:

```bash
python3 tools/provision.py doctor            # what is here, and what to do about what is not
python3 tools/provision.py doctor --install   # do it
```

`doctor` exists because "install them" is the least useful thing to say to somebody who is
stuck, and because two things go wrong here that both leave a tool looking installed and not
working:

* **The executable is not the import.** `pip install mpremote` puts a script in one
  interpreter's scripts directory. If that directory is not on PATH the install succeeds and
  the wizard still cannot find it, which reads as the install having failed. So the plan says
  where the script will land and whether that place is on PATH *before* anything runs, and
  `--install` looks again afterwards rather than trusting pip's exit code.
* **More than one copy can be installed.** On a machine with several Pythons, the one that
  answers to `python3` is often not the one whose scripts directory holds the tools. Every copy
  on PATH is reported, in the order that decides which one runs.

`esptool` answers to both `esptool` and `esptool.py` depending on its version; either is
accepted. It is only needed when you pass `--firmware`, and the wizard checks for it before a
phase starts rather than halfway through, so a flash never fails after a board has been half
configured.

## Why it lives inside the library

It has to run the library's own crypto. It mints P-256 control roots and derives `device_id`
fingerprints on the operator's machine that must match, byte for byte, what the board computes.
A second copy of that code would drift silently and surface as a node that will not handshake.

## The gesture

**One value per node, copied once.** The Hub needs the Edge's `device_id`; the Edge needs
nothing about the Hub. `edge` reads that fingerprint off the board and records it in the fleet;
`hub` reads it back out and writes it into the `Nodes.json` it pushes. Nobody copies a value
between two terminals, and no `main.py` carries a pasted constant.

## The fleet directory

`--fleet` (default `./allora-fleet`) is where a deployment's authority lives:

```
allora-fleet/
  control_root.key    the signing half. This deployment's authority.
  control.counter     the highest number minted under it, keyed by its fingerprint.
  fleet.json          the nodes issued so far: the operator's record.
  backups/            every identity.key saved before a flash erased it.
  staging/            exactly what was pushed to each board, kept so it can be looked at later.
```

A visible directory rather than a hidden one under `$HOME`, because the root and its counter
are files somebody has to hand over when a deployment moves from the bench to a backend.

**Exactly one signer per root.** `control.counter` holds the highest number minted. Two signers
on one root both start at zero, both mint number 1, and the target refuses the second as a
replay, which looks exactly like the command not working. So handover moves `control_root.key`
and `control.counter` together, or it moves neither, and the previous signer stops.

## Where the control root lives

The rule that generates the right answer every time: **the control root lives with the
operator, not with the radio.** Ask who decides to retune this node; that is where the private
half goes, and everything between that decision and the node is a courier, the Hub included.

| mode | root lives on | the Hub is | when |
|---|---|---|---|
| **offline root** (default) | the operator's machine, or the website backend | a courier | every deployment the paper describes |
| **on-site root** (`--on-site-root`) | the machine running Hub logic, a disconnected Pi included | the authority | a site that must issue novel commands with no backhaul |

On-site root is a real, supported mode. What it does not get is silence: choosing it prints
what it costs. A compromised Hub can then create commands rather than only relay them.

## Three rules the code keeps so nobody has to remember them

**The identity key is made on the board.** `load_or_create_identity` runs on the device; the
wizard asks for the fingerprint and never mints a key on the laptop. That is what makes a
`device_id` something the board proves it holds.

**The identity is backed up before the flash.** The flash erases the board filesystem, and the
`device_id` is derived from a key that lives on it. A board whose identity cannot be *read* is
not flashed at all, because "I could not look" reported as "there was nothing there" ends with
a working board, a new identity, and a Hub that never hears from the node it registered.

**A commanded node gets the verifying half and only the verifying half.** 64 hex characters is
the signing scalar, 130 is the verifying key, same filename on both node kinds. The signing
half on a field node is a key compromise hiding behind a working link.

## The T3S3 gotchas, encoded rather than documented

The ESP32-S3 uses native USB, and everything follows from that: the port re-enumerates on a
hard reset, `ampy` hangs on this REPL, `esptool`'s auto-reset does not work. So the wizard uses
`mpremote` for every file operation, enters download mode over the wire
(`machine.bootloader()`) with the BOOT/RESET hold as the fallback, chains erase and write so
the board stays in the loader between them, polls for the REPL afterwards instead of assuming
it is back, and runs the verify scripts with `mpremote run`, which soft-resets and so does not
drop the port.

## `--json`

Every command takes it. Under `--json`, stdout carries exactly one document and progress goes
to stderr, so a caller parses stdout without filtering it. This is the integration surface for
the control website, which is TypeScript and shells out.

```json
{"command": "edge", "ok": true, "error": null,
 "steps": [{"step": "device_id", "status": "ok", "detail": "d909f4eb…"}],
 "warnings": [], "data": {"device_id": "d909f4eb…", "posture": "secure"}}
```

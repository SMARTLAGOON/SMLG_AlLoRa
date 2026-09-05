# `tools/` — the provisioning wizard

Host-side code. CPython only, never frozen into firmware: the manifest freezes `AlLoRa/`, so
nothing here reaches a board.

Start here, with both boards plugged in:

```bash
python3 tools/provision.py setup
```

`setup` is the guided path and the only interactive command. It checks the machine, finds the
boards and shows you their MACs, asks what this deployment is (posture, which board is the
Edge, what each board is, firmware), prints the plan, and runs it once you say yes. It ends by driving one
real 1000-byte transfer, because "provisioned" otherwise means only that files were copied.

`setup` writes down what it was told to do before it does any of it, as a **plan file**, and
then applies it. That is the same file `provision apply` runs, so a run stops being a
conversation nobody can repeat:

```bash
python3 tools/provision.py setup                       # asks, writes allora-fleet/plan.json, runs it
python3 tools/provision.py apply allora-fleet/plan.json --json   # runs it again, asking nothing
```

Underneath it is a toolkit, and every phase is also its own non-interactive command. That is
what the control website drives, and what to reach for when a run needs a flag `setup` does
not ask about:

```bash
python3 tools/provision.py fleet-init
python3 tools/provision.py edge --firmware AlLoRa-t3s3-sx127x-firmware.bin
python3 tools/provision.py hub  --firmware AlLoRa-t3s3-sx127x-firmware.bin
python3 tools/provision.py verify --edge-port /dev/cu.usbmodemAAAA \
                                  --hub-port  /dev/cu.usbmodemBBBB
```

`setup` drives exactly those phases. It is a front end, not a second implementation.

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
  plan.json           what the last run set out to do: its intent.
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

**Coming back from a flash is its own step.** The write ends with `--after hard_reset` and on
this hardware the board still comes back silent: enumerated, present in `/dev`, answering no
REPL. So the wait escalates rather than giving up. It probes, then issues a reset over the wire
with a second `esptool` call, which is what actually brings the board back, and only then asks
for a tap on RESET. Under `setup` that tap is a prompt inside the run; under the plain commands,
where there may be nobody at the keyboard, it is the same refusal it has always been. Measured
on the bench on 2026-08-27: five flashes, the board never returned by itself, the wire reset
recovered it every time it was tried.

## The board decides where the radio is wired

`--board` (default `t3s3`) names the hardware that runs the node. It is a separate question
from `--radio`, and it has to be, because **a board does not imply its radio and a pin is a
board fact rather than a radio fact**: the bench pair is two T3-S3s carrying different chips,
and the same SX1262 sits on clk 5 on a T3S3 and on clk 9 on a Heltec LoRa32 V3.

A board row says what that product has soldered in, what it can host on a header, and where
each of those meets it. That gives two refusals, and they are deliberately different. A board
that cannot carry the radio at all is refused permanently, because a bare transceiver is part
of a board's design and never something plugged into a header. A board that can carry it with
nobody having recorded the pins, an E5 on a T3-S3 today, is refused for now, and the message
says the combination is real and asks for the measurement.

Board names are a closed set with aliases, because the point is refusing what cannot be wired
and a free-form name cannot be checked. `t3s3-epaper` resolves to the `t3s3` row: the two wire
their radios identically and differ only in the screen. **The registry records the name the
operator gave, not the row it resolved to**, so when a revision moves a pin it can still say
which nodes are affected.

The wizard writes the board's pin map into the connector block for the radios whose connector
reads pins from there, which today is `sx1262` alone. `sx127x` takes its pins from its own
driver's board file, so none are written for it: a number in a config that nothing reads is the
same defect seen from the other end.

**Why this is a flag and not a default buried in a driver.** `SX1262_connector` answers a
missing pin with the Heltec map. So a config that names no pins does not fail loudly, it
provisions a board that cannot find its own chip and reports every step as ok. On the bench
this looked like a fully successful `provision edge` followed by a node asserting
`ERR_CHIP_NOT_FOUND` at boot. A board with no pin map here is refused rather than guessed at,
for the same reason.

## `--json`

Every command takes it. Under `--json`, stdout carries exactly one document and progress goes
to stderr, so a caller parses stdout without filtering it. This is the integration surface for
the control website, which is TypeScript and shells out.

```json
{"command": "edge", "ok": true, "error": null,
 "steps": [{"step": "device_id", "status": "ok", "detail": "d909f4eb…"}],
 "warnings": [], "data": {"device_id": "d909f4eb…", "posture": "secure"}}
```

## The plan file

The plan is the unit of provisioning, and there are two front doors onto one engine. `setup`
writes a plan as it asks its questions and then applies it; `apply` runs a plan somebody else
wrote, a website included. Same file, same engine, and the interactive path gains an artefact
the operator can keep, diff, re-run and hand to somebody else.

```json
{
  "version": 1,
  "mode": "scratch",
  "fleet": "allora-fleet",
  "posture": "secure",
  "session_id": null,
  "rf": {"sf": 7, "freq": 868, "bandwidth": 125},
  "edges": [{"mac": "4a274ae0", "name": null, "board": "t3s3-epaper",
             "radio": "sx1262", "firmware": null}],
  "hub": {"mac": "9eeff0e0", "name": null, "board": "t3s3", "radio": "sx127x",
          "firmware": "firmware/_dl_33558825130/AlLoRa-t3s3-sx127x-firmware.bin"}
}
```

**Each node names its own board, radio and firmware. The RF settings stay run-level.** The pair
above is the bench pair: an SX1262 Edge and an SX127x Hub. One radio for the whole run could not
describe it, and a plan that tried wrote one of the two boards a driver for a chip it does not
have. Which chip a node has and where it is attached is a fact about that node; how the radio is
tuned is a fact about the deployment, because `sf`, `freq` and `bandwidth` have to agree across
the pair or the two ends do not hear each other. A per-node tuning would be a way to build a
fleet that cannot talk, so `rf` must never move into an entry.

**Firmware is per node for the same reason, and it is the more dangerous half.** Per-node radio
with one image for the run would let a plan describe the mixed pair and then flash the SX1262
board with the SX127x build, with every step still reporting success. `setup` picks each node
the newest local build whose filename names that node's target, `<board>-<radio>`, and a node
whose target has no build on this machine keeps what it is running and is told so before the
plan is approved.

**A plan may be written before the hardware is on the desk.** Board, radio, role, name, posture
and RF are design-time facts, which somebody planning a deployment knows; which physical unit
fills a slot is a bench-time fact, which they do not. So `mac` may be `null`: the document is
valid, `provision setup` binds it to the boards answering, and `apply` refuses an unbound plan
and names the slots that need a board. Nothing is ever flashed against a slot nobody has filled.

**A plan names boards by MAC, never by port.** The port is not an identity: on native USB it
re-enumerates on every hard reset, so a plan recording ports would aim Monday's intent at
whichever board happened to land on that path on Tuesday. `provision ports` prints the MAC of
every board answering. A plan naming a board nobody plugged in fails at the desk, before the
first board is touched, rather than after one end is provisioned and the other is not.

**`mode` is the first field, and the first question `setup` asks.** It is only asked when the
fleet already holds nodes, because on an empty one there is nothing to extend.

| mode | what it does | what it costs |
|---|---|---|
| `scratch` | provisions both ends and builds the Hub from nothing | the deployment is new; a fleet directory is never written over, so starting a second one asks for its own |
| `extend` | provisions the new nodes and **updates the Hub's roster only** | the Hub is not reflashed, not reconfigured, and keeps its identity: adding a node changes one row in one file |

Extend is adding an Edge, replacing a broken node, or swapping a Hub into a deployment that is
already running. It is what a `setup` run into a non-empty fleet used to do by accident, which
orphaned a record and appended a phantom the Hub then spent a listening window polling every
cycle.

**An Edge's `name` is what says which of those this is.** A name the fleet does not hold is a
new node; a name it already holds is a replacement, and the board answering under it takes that
slot. That is the whole difference between the two cases, because a name *is* a slot: the
registry keys a node by its role and its name, so registering under the old one updates that
record rather than adding beside it, and the roster row keyed to the dead fingerprint is
replaced by the row keyed to the new one. `setup` asks; a plan written elsewhere says it by
choosing the name.

**A posture change warns and guides; it does not refuse.** Moving a deployment between open,
secure and control means reflashing every node, because the posture is what a node was
provisioned with. A tool that refused would send the operator to do exactly that by hand, one
board at a time, which is the outcome the refusal was trying to prevent. So `setup` says which
nodes stay on the old posture and stop being part of the working deployment, and then does what
it was asked.

## Why extend does not rebuild `Nodes.json`

**`Nodes.json` is a read/write state file, and the Hub is the other writer.** When a node
accepts a retune, `Hub._persist_endpoint_rf` writes that endpoint's settled radio settings back
into the roster. So the Hub's copy, not the operator's registry, is the truth about what an
Edge in the field is listening on.

Rebuilding that file from `fleet.json` would put the Hub back on the settings an Edge was
issued a year ago and has since left. Nothing on either side would say so: the Hub simply polls
an address nobody answers on. So an extend run reads the roster off the Hub, writes only the
rows for the Edges it just provisioned, carries every other row across untouched, and prints
what it kept:

```
[2/3] Hub roster on /dev/cu.usbmodem101
      kept        S on sf 9, freq 869, bandwidth 125, as this Hub has it
      Nodes.json  1 row(s) written, 1 left as the Hub has them
      restart     polling the roster it was just given
```

The restart is what makes the new row take effect: a Hub registers its endpoints once, from the
file it read at boot. It also closes the read-modify-write window, because `mpremote` interrupts
the running program to reach the filesystem, so the Hub is stopped from the read through to the
write and cannot settle a trial into the copy being replaced.

**Three files, three lifetimes, and no conflict between them:** the plan (your machine,
*intent*), `fleet.json` (your machine, *record*), `Nodes.json` (on the Hub board, *live*, and
mutated by the Hub itself). Each is allowed to disagree with the one before it, because each
answers a different question.

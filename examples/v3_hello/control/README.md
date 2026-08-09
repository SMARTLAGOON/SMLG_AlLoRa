# Control: let the Hub retune the Edge over the air

Changing the radio settings in the other two postures means editing two files and rebooting two
boards. A Hub can also do it over the air:

```python
hub.ask_change_rf(endpoint, {"sf": 9, "trial": 300})
```

That call exists in all three postures. What differs is **how the command is authenticated**, and
that follows entirely from what the nodes were provisioned with. Provisioning is the whole of the
setup:

* **Nothing provisioned** ([`../open`](../open), [`../secure`](../secure)): the command travels in
  band on the link itself, unsigned. Fine for a survey run, where the link authenticates nothing
  anyway.
* **A control root provisioned** (this folder): the command is a signed artifact, and the node
  then refuses unsigned ones. That refusal is the point of provisioning: if an unsigned frame
  still worked, the signature would be protecting nothing.

Signed control needs `"security_mode": "secure"` on both nodes, because an artifact names the
node it is for by its `device_id`, and an open node has none. Both `LoRa.json` files here are the
secure ones with one line added.

## Create the fleet's root

Once, on your laptop:

```bash
python3 provision_control_root.py
```

That writes `hub/control_root.key` (the signing half, 64 hex characters) and
`edge/control_root.key` (the verifying half, 130). Same filename on both, like `identity.key`:
what the file *contains* is what decides whether a node signs commands or checks them. Running it
again never replaces an existing root, so adding a node later is safe.

Copy each half to its own board:

```bash
mpremote connect /dev/cu.usbmodemHUB  fs cp hub/control_root.key  :control_root.key
mpremote connect /dev/cu.usbmodemEDGE fs cp edge/control_root.key :control_root.key
```

Both `LoRa.json` files here already carry the line that activates it:

```json
  "control_root_file": "control_root.key",
```

That one line is the whole provisioning. Both nodes then keep a counter file beside the key
without being told to, in `control.counter`: the Hub remembers how many commands it has issued
under this root, and the Edge remembers the highest one it has accepted. That is what stops a
command being recorded off the air and replayed back later, and it is why a reboot on either end
does not leave the pair unable to talk. Set `control_counter_file` if you want the file somewhere
else.

Then register the Edge by its `device_id` in `hub/main.py`, exactly as in
[`../secure`](../secure).

## Three things that will bite otherwise

1. **The private half never goes on an Edge.** A node handed it refuses to boot and says so,
   because that key signs for the whole fleet and it has no business on a node in the field.
2. **A named file that is missing is a startup error, not a warning.** Unlike `identity.key`, a
   control root is never generated on the board: a node that invented its own would be its own
   authority. If the config names one, the node halts until it is there.
3. **Keep the root.** Every node is pinned to it. Replacing it means re-provisioning every board
   by hand, which is also the deliberate way to reset a fleet's command counters.

## Running it

Load the two `main.py` + `LoRa.json` pairs as usual, plus the two `control_root.key` halves.

`hub/main.py` pulls one file first so the link is known good, commands the retune, and keeps
polling. Watch the Edge's serial for `Changing RF Config to:` and then, once a whole file has
crossed on the new settings, `RF trial committed`.

If the new configuration cannot carry a transfer, nobody has to intervene: the Edge holds it only
for `trial` seconds, rolls back to the last configuration that worked, and the Hub's `{new, old}`
probe finds it there. That undo is the reason a retune is safe to try at all.

## The four words `ask_change_rf` answers with

Each one is a different next move.

| It says | It means | What to do |
|---|---|---|
| `accepted` | The Edge acknowledged the command and both ends moved. | Nothing. |
| `pending` | A signed artifact is queued. It is handed over on one of the pulls, and the Edge decides afterwards, on its own. | Keep polling and read `hub.rf_change_status(endpoint)`, which the probe settles a few visits later. |
| `refused` | The Edge answered a poll, so it is there and listening, and it declined the command. | Look at provisioning, not at the radio. |
| `unreachable` | Nothing answered, command or poll. | Look at the link. The command was never considered. |

**`pending` is the normal answer in this posture**, because a signed artifact is not acted on
where it is handed over.

**`refused` here points at the root itself.** A node checks against the root it was given, so an
Edge carrying a different fleet's verifying half refuses every command this Hub signs. Compare
the fingerprints that `provision_control_root.py` prints.

Telling `refused` from `unreachable` is why the Hub asks one extra short question after a command
goes unanswered, and only then. A refusal and a broken antenna produce exactly the same silence,
and reporting them as one thing sent people to check hardware when the repair was a missing key.

## The half-provisioned state, and why this example stops

A node can hold a control root while its config still says `open`. That is the state of a fleet
whose nodes already carry the root but whose configs have not been moved to secure yet, and
`edge/main.py` halts on it with an explanation rather than running.

No verify gate can be built there, and that is a property of the design rather than a gap: a
signed artifact is addressed to a 32-byte `device_id`, and an open node has no identity to be
addressed by. Such a node can only ever do the refusing half of holding a root. The refusing half
does work, and it is the useful half, but a node left running like that looks provisioned and can
never accept a command, so halting is the honest answer. Give it `identity_file` and
`security_mode: "secure"` to finish the job.

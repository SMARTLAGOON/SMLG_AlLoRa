# Open mode: a file across, with nothing underneath it

The starting posture. No identity, no keys, frames in the clear. This is the honest shape for a
survey link, which authenticates nothing in either direction, and it is the fastest way to prove
two boards can hear each other before anything else is in the way.

Load and run it as described in [the parent README](../README.md), copying from `open/`.

## The thing to line up here

**`session_id` must match**, because v3 addresses by session id, not MAC. Both files say `42`.

The Hub's `mac_address` field is only a label and the save-folder name; it does not affect
addressing in v3. Set it to the Edge's printed MAC if you want a meaningful folder.

If you leave `session_id` out of `AlLoRa.json` entirely, the Edge derives one from the low byte of
its own short MAC, and a MAC-registered endpoint on the Hub derives the same value. That works,
but it means the Hub has to know the Edge's real MAC, so the explicit `42` here keeps the example
readable on any two boards.

## What you should see

The Edge prints `EDGE ready`, its MAC, `mode: open` and `session: 42`, then the folder it serves
from. The Hub prints `HUB ready` and one `polling` line for the Edge in `Nodes.json`. With
`"debug": true` both then print each step of the exchange, and the Hub saves each finished file
under `Results/<label>/`, here `Results/9eeff0dc/`. The Edge deletes a file from `Outbox` once the
Hub has confirmed it.

## Control commands in open mode

The shared [`../main.py`](../main.py) attaches a `Node_Control_Actuator` to a node holding no
control root, so the Hub can retune this Edge over the air with
`hub.ask_change_rf(endpoint, {"sf": 9, "trial": 300})`. The command travels in band on the link
itself, unsigned, and nothing authenticates it. That is the trade an open link already made
everywhere else.

A node given no actuator ignores every command instead, which is the other reasonable
choice. What you cannot get in this posture is a command a node can actually check: that needs a
signature, a signature needs an address, and an open node has no identity to be addressed by. See
[`../control`](../control).

# Secure mode: the same transfer, sealed

Each node gets a crypto identity. The ECDH handshake runs automatically on first contact and
every frame after it is AEAD-sealed. This needs the CTR-flag firmware, which the CI build has.

Load and run it as described in [the parent README](../README.md), copying from `secure/`.

## The thing to line up here

**Register the Edge by its `device_id`, not a MAC.** On boot the Edge prints:

```
EDGE device_id (register this on the Hub): <hex>
```

Paste that value into `secure/hub/main.py`:

```python
endpoint = Digital_Endpoint(name="src", device_id="<the printed device_id>", active=True)
```

That is the whole of the pairing. First contact is addressed by `device_id[:4]` (no MAC on the
wire), and the session id derives from the same identity on both ends, so unlike
[`../open`](../open) there is **no `session_id` to keep in sync**. One value registers a node
instead of two. That `device_id[:4]` is also what names the Hub's save folder.

## Why `identity_file` matters

Both `LoRa.json` files here carry `"identity_file": "identity.key"`. A node generates that key on
first boot if it is missing, so the `device_id` you paste into the Hub stays valid across reboots
only as long as that file survives.

Erasing the board's filesystem, which a full reflash does, takes the identity with it. The Edge
comes back with a new one, prints a different `device_id`, and the Hub goes on addressing an
identity that no longer exists. The symptom is a link that looks dead in both directions with no
error on either side, so re-read the Edge's boot line after any reflash.

## What you should see

Watch the Edge's serial. Instead of `Could not parse frame`, it answers the handshake CTRL
frames, and then the sealed transfer runs exactly like the open one.

## Control commands in secure mode

`secure/edge/main.py` attaches a `Node_Control_Actuator`, so `hub.ask_change_rf(...)` works here
too. The sealed link means a command that arrives came from the node that completed the
handshake, which is a real improvement on open mode.

It is still not a signed artifact: it is not checked against a fleet authority, and it does not
survive being relayed by anything other than the peer that sealed it. For that, and for a node
that refuses unsigned commands outright, see [`../control`](../control).

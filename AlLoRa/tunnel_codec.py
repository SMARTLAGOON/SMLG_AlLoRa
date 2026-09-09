"""tunnel_codec: how a transport call is spoken on the tunnel link.

`Codec` speaks `Packet`s on the LoRa air wire. This speaks *transport calls* on the link
joining the two halves of a split Connector. Same job on a different pipe, which is why it
carries the same name: encode the thing, move it, decode it on the far side.

What crosses is a **remote procedure call (RPC)**: a call that looks local to whoever makes it
but actually runs on another machine, with its arguments and its return value shipped across a
wire in between. The logic-holder calls `connector.transmit(wire)` as though it owned a radio.
It does not. This module encodes that call, the Link carries the bytes, and the bridge half
decodes it, runs the real method on the real radio, and sends the return value back the same
way. Six procedures, matching the Connector's transport surface: transmit / listen / exchange,
plus get/set RF config and get MAC.

This module is only the (de)serialization of those calls: no I/O, no radio, no crypto, no
keys. So it unit-tests on CPython, and a Link has only to move the opaque request/reply bytes
it produces, whatever medium that Link happens to run on.

The bytes between the halves are *not* the LoRa air wire: the LoRa frame rides inside as an
opaque hex blob, so v2 / v3-open / v3-secure all cross unchanged and the bridge never parses
one or holds a key. JSON keeps it debuggable and lets every Link serialize the same dict; the
radio payload is <=255 B, so hex-doubling on the local link (fast relative to airtime) is
cheap. `null` in a wire field means "no frame" (a radio timeout), which is why a lost reply
stays distinct from a zero-length one.

**Every call carries an id, and its reply carries the same one back.** Without it a reply says
nothing about what it answers, and the client can only assume that the next frame up belongs to
the last frame down. That assumption breaks whenever the bridge is still finishing an earlier
call: a `listen` or `exchange` blocks at the radio for the length of its window, so a client
that gave up, or a client that restarted, leaves a well-formed reply still to come. It arrives
against the *next* call and is indistinguishable from its answer. Between two different verbs
that surfaces as nonsense (an RF question answered with a radio frame). Between two `exchange`
calls, which is the common case in a transfer, it does not surface at all: the wrong chunk is
simply accepted. The id is what makes the two cases one case, and a checkable one.

The id is the client's to mint and the bridge's only to echo, so the bridge stays as dumb as it
was. It is absent rather than null when unused, which is what lets the two halves be upgraded
one at a time: a bridge that predates the id echoes nothing and its replies are taken on trust
exactly as they always were, and a client that predates it sends nothing for a new bridge to
echo. Neither half has to know which vintage the other is.

The signal readings ride home the same way. A bridge measures RSSI and SNR at its radio for
every frame it receives, and until they travelled in the reply they stayed on the bridge: the
logic-holder inherited the base Connector's stub and reported 0 dBm for hardware it had heard
perfectly. They belong in the reply and not in a question of their own, because a separate
question answers about whatever the radio heard most recently, which need not be the frame
being asked about. `null` when no frame arrived: a window that heard nothing has no signal to
report, and a fabricated number there is worse than an absent one.
"""
from AlLoRa.utils.json_utils import json

# Verbs: the transport surface a split Connector's bridge half (an Adapter) serves.
TRANSMIT = "tx"
LISTEN = "ln"
EXCHANGE = "xc"
SET_RF = "rf"
GET_RF = "grf"
GET_MAC = "mac"

# The call id, on the request and echoed on its reply.
REQ_ID = "i"


def _hex(b):
    # bytes -> hex str; None (a radio timeout / absent frame) -> None, so it survives as null.
    return b.hex() if b is not None else None


def _unhex(s):
    # hex str -> bytes; null or "" -> None (no frame). A real frame is never zero-length.
    return bytes.fromhex(s) if s else None


def _dumps(d):
    return json.dumps(d).encode()


def _loads(b):
    return json.loads(b.decode() if isinstance(b, (bytes, bytearray)) else b)


def _tagged(d, req_id):
    # Absent, not null, when there is no id: an un-numbered frame is how a half that predates
    # the id looks, and the matching rule reads absence as "cannot be checked" rather than as
    # a mismatch. Writing null instead would make the two indistinguishable.
    if req_id is not None:
        d[REQ_ID] = req_id
    return d


# --- requests (logic-holder -> bridge) ---

def encode_transmit(wire, req_id=None):
    return _dumps(_tagged({"v": TRANSMIT, "wire": _hex(wire)}, req_id))


def encode_listen(window, req_id=None):
    return _dumps(_tagged({"v": LISTEN, "w": window}, req_id))


def encode_exchange(wire, window, match_prefix, req_id=None):
    return _dumps(_tagged({"v": EXCHANGE, "wire": _hex(wire), "w": window,
                           "m": _hex(match_prefix)}, req_id))


def encode_set_rf(freq=None, sf=None, bw=None, cr=None, tx=None, req_id=None):
    return _dumps(_tagged({"v": SET_RF, "freq": freq, "sf": sf, "bw": bw, "cr": cr, "tx": tx},
                          req_id))


def encode_get_rf(req_id=None):
    return _dumps(_tagged({"v": GET_RF}, req_id))


def encode_get_mac(req_id=None):
    return _dumps(_tagged({"v": GET_MAC}, req_id))


def decode_request(frame):
    """Parse a request frame -> (verb, args). `args` normalises the wire fields back to bytes
    (`wire`, `match_prefix`) and exposes the window as `window` and the call id as `req_id`, so
    the Adapter dispatches, and echoes, without touching the JSON shape."""
    d = _loads(frame)
    verb = d.get("v")
    args = dict(d)
    if "wire" in d:
        args["wire"] = _unhex(d["wire"])
    if "m" in d:
        args["match_prefix"] = _unhex(d["m"])
    if "w" in d:
        args["window"] = d["w"]
    args["req_id"] = d.get(REQ_ID)
    return verb, args


# --- replies (bridge -> logic-holder) ---

def reply_id(frame):
    """The id a reply is answering, or None when it carries none (an older bridge, or a frame
    that is not a reply at all). Read on its own, before the reply is decoded as any particular
    verb, because deciding *whether* this is the answer comes first."""
    try:
        return _loads(frame).get(REQ_ID)
    except Exception:
        return None


def encode_bool_reply(ok, req_id=None):
    return _dumps(_tagged({"ok": bool(ok)}, req_id))


def decode_bool_reply(frame):
    return bool(_loads(frame).get("ok"))


def encode_listen_reply(wire, td, rssi=None, snr=None, req_id=None):
    return _dumps(_tagged({"wire": _hex(wire), "td": td, "rssi": rssi, "snr": snr}, req_id))


def decode_listen_reply(frame):
    d = _loads(frame)
    return _unhex(d.get("wire")), d.get("td"), d.get("rssi"), d.get("snr")


def encode_exchange_reply(wire, td, status, rssi=None, snr=None, req_id=None):
    return _dumps(_tagged({"wire": _hex(wire), "td": td, "st": status,
                           "rssi": rssi, "snr": snr}, req_id))


def decode_exchange_reply(frame):
    d = _loads(frame)
    return _unhex(d.get("wire")), d.get("td"), d.get("st"), d.get("rssi"), d.get("snr")


def encode_get_rf_reply(rf_config, req_id=None):
    return _dumps(_tagged({"rf": list(rf_config)}, req_id))


def decode_get_rf_reply(frame):
    return _loads(frame).get("rf")


def encode_get_mac_reply(mac, req_id=None):
    return _dumps(_tagged({"mac": mac}, req_id))


def decode_get_mac_reply(frame):
    return _loads(frame).get("mac")

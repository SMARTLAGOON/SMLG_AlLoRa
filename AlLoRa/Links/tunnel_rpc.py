"""tunnel_rpc: the host<->bridge line protocol for the split Connector.

The tunnel carries the *transport verbs* (transmit / listen / exchange + RF config) across a
serial or WiFi link: the logic-holder half forwards a verb call *down*, the bridge (Interface)
half runs it on the real radio and returns the result *up*. This module is the pure
(de)serialization of those calls (no I/O, no radio, no codec, no keys), so it unit-tests on
CPython and the transport Link only has to move the opaque request/reply bytes.

The bytes *between* host and bridge are not the LoRa air wire: the LoRa frame rides inside as
an opaque hex blob, so v2 / v3-open / v3-secure all cross unchanged (the bridge never parses
it, holds no key). JSON keeps it debuggable and lets the serial and WiFi links serialize the
same dict; the radio payload is <=255 B, so hex-doubling on the local link (fast relative to
airtime) is cheap. `null` in a wire field means "no frame" (a radio timeout), which is why a
lost reply stays distinct from a zero-length one.
"""
from AlLoRa.utils.json_utils import json

# Verbs: the transport surface a split Connector's Interface serves.
TRANSMIT = "tx"
LISTEN = "ln"
EXCHANGE = "xc"
SET_RF = "rf"
GET_RF = "grf"
GET_MAC = "mac"


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


# --- requests (logic-holder -> bridge) ---

def encode_transmit(wire):
    return _dumps({"v": TRANSMIT, "wire": _hex(wire)})


def encode_listen(window):
    return _dumps({"v": LISTEN, "w": window})


def encode_exchange(wire, window, match_prefix):
    return _dumps({"v": EXCHANGE, "wire": _hex(wire), "w": window, "m": _hex(match_prefix)})


def encode_set_rf(freq=None, sf=None, bw=None, cr=None, tx=None):
    return _dumps({"v": SET_RF, "freq": freq, "sf": sf, "bw": bw, "cr": cr, "tx": tx})


def encode_get_rf():
    return _dumps({"v": GET_RF})


def encode_get_mac():
    return _dumps({"v": GET_MAC})


def decode_request(frame):
    """Parse a request frame -> (verb, args). `args` normalises the wire fields back to bytes
    (`wire`, `match_prefix`) and exposes the window as `window`, so the Interface dispatches
    without touching the JSON shape."""
    d = _loads(frame)
    verb = d.get("v")
    args = dict(d)
    if "wire" in d:
        args["wire"] = _unhex(d["wire"])
    if "m" in d:
        args["match_prefix"] = _unhex(d["m"])
    if "w" in d:
        args["window"] = d["w"]
    return verb, args


# --- replies (bridge -> logic-holder) ---

def encode_bool_reply(ok):
    return _dumps({"ok": bool(ok)})


def decode_bool_reply(frame):
    return bool(_loads(frame).get("ok"))


def encode_listen_reply(wire, td):
    return _dumps({"wire": _hex(wire), "td": td})


def decode_listen_reply(frame):
    d = _loads(frame)
    return _unhex(d.get("wire")), d.get("td")


def encode_exchange_reply(wire, td, status):
    return _dumps({"wire": _hex(wire), "td": td, "st": status})


def decode_exchange_reply(frame):
    d = _loads(frame)
    return _unhex(d.get("wire")), d.get("td"), d.get("st")


def encode_get_rf_reply(rf_config):
    return _dumps({"rf": list(rf_config)})


def decode_get_rf_reply(frame):
    return _loads(frame).get("rf")


def encode_get_mac_reply(mac):
    return _dumps({"mac": mac})


def decode_get_mac_reply(frame):
    return _loads(frame).get("mac")

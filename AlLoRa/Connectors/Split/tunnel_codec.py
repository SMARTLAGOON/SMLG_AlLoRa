"""Encodes radio commands as JSON for the Link between the two halves of a split Connector.

The LoRa frame travels inside as hex, so the radio side never reads it or holds a key. Each reply
carries its request's id, so a late reply is not taken as the next answer. Requests without an id
still work, so either side can be upgraded first.
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

"""Names the file that carries one MQTT message: `mq!<artifact_id>!<timestamp_ms>!<topic>`.

The topic travels in the file name, and the file holds the MQTT payload unchanged. The name is sent
in one METADATA frame, so the topic must fit in it. `/`, `!` and `%` in the topic are escaped,
because the receiver also uses the name as a file path.
"""

_PREFIX = "mq!"
_HEX = "0123456789ABCDEF"


def _escape(topic):
    out = []
    for ch in topic:
        if ch in "%!/":
            b = ord(ch)
            out.append("%" + _HEX[b >> 4] + _HEX[b & 0xF])
        else:
            out.append(ch)
    return "".join(out)


def _unescape(text):
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "%":
            if i + 3 > len(text):  # need two hex digits after '%'
                raise ValueError("truncated escape in envelope topic")
            out.append(chr(int(text[i + 1:i + 3], 16)))
            i += 3
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def is_envelope(name):
    """Cheap shape check only: decode_name still raises on a malformed body, so a plain
    file that happens to start with the prefix falls back loudly, not silently."""
    return isinstance(name, str) and name.startswith(_PREFIX)


def encode_name(topic, artifact_id, timestamp_ms=None):
    ts = "" if timestamp_ms is None else str(int(timestamp_ms))
    return _PREFIX + str(int(artifact_id)) + "!" + ts + "!" + _escape(topic)


def decode_name(name):
    """-> (topic, artifact_id, timestamp_ms|None). Raises ValueError on anything that
    doesn't parse: the caller treats that as a plain (non-envelope) filename."""
    if not is_envelope(name):
        raise ValueError("not an MQTT envelope name")
    parts = name.split("!", 3)
    if len(parts) != 4:
        raise ValueError("malformed MQTT envelope name")
    _, id_field, ts_field, topic_field = parts
    try:
        artifact_id = int(id_field)
        timestamp_ms = int(ts_field) if ts_field else None
    except ValueError:
        raise ValueError("malformed MQTT envelope fields")
    return _unescape(topic_field), artifact_id, timestamp_ms

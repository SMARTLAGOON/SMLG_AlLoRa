"""Message templates: the shape of the message a finished file becomes, written by a deployment.

A template is JSON with placeholders such as `@file.name`. It is parsed and checked once when it
is loaded, so a typo halts at startup instead of silently changing what a consumer receives.
Formatting a message is all this does: it knows nothing about where the message goes.
"""
try:
    import utime as time
except ImportError:
    import time
try:
    import ubinascii as binascii
except ImportError:
    import binascii

from AlLoRa.DataSources.mqtt_naming import is_envelope, decode_name
from AlLoRa.utils.json_utils import json

# The closed vocabulary. Each name describes one file's arrival: nothing plural or historical.
PLACEHOLDERS = (
    "@node.label", "@node.device_id", "@node.session_id", "@node.mode",
    "@file.name", "@file.size", "@file.chunks_total", "@file.arrival",
    "@file.content.text", "@file.content.base64",
    "@file.stats.rssi", "@file.stats.snr",
    "@file.origin.topic", "@file.origin.artifact", "@file.origin.observed",
    "@hub.site", "@hub.published",
)

_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_."


class Unpublishable(Exception):
    """This file can't be put in this shape, e.g. `@file.content.text` on a JPEG. The file
    is not published; it is already archived, so nothing is lost."""


def _split(text):
    """One template string as pieces: plain text, and placeholders as 1-tuples.
    `@@` is a literal `@`."""
    pieces = []
    plain = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "@":
            plain += ch
            i += 1
            continue
        if text[i + 1:i + 2] == "@":
            plain += "@"
            i += 2
            continue
        j = i + 1
        while j < len(text) and text[j] in _NAME_CHARS:
            j += 1
        # A sentence may end right after a placeholder: "sent by @node.label."
        while j > i + 1 and text[j - 1] == ".":
            j -= 1
        if plain:
            pieces.append(plain)
            plain = ""
        pieces.append((text[i:j],))
        i = max(j, i + 1)
    if plain:
        pieces.append(plain)
    return pieces


def _iso(ms):
    """Milliseconds since the epoch as ISO-8601 UTC, to the second."""
    t = time.gmtime(int(ms) // 1000)
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(*t[:6])


def _iso_ms(ms):
    """The same, keeping milliseconds: a source's own timestamp, which it chose to send."""
    return "{}.{:03d}Z".format(_iso(ms)[:-1], int(ms) % 1000)


class _Arrival:
    """What the placeholders read for one file. The content is read at most once."""

    def __init__(self, file, reception, hub):
        self.file = file
        self.reception = reception
        self.hub = hub or {}
        self._content = None

    def content(self):
        if self._content is None:
            self._content = bytes(self.file.get_content())
        return self._content

    def value(self, placeholder):
        if placeholder.startswith("@file.origin."):
            return self.origin()[placeholder]
        return _READERS[placeholder](self)   # every name was checked at load

    def device_id(self):
        did = self.reception.device_id
        return did.hex() if did is not None else None

    def arrival(self):
        ms = self.reception.timestamp_ms
        return _iso(ms) if ms is not None else None

    def text(self):
        try:
            return self.content().decode("utf-8")
        except UnicodeError:
            raise Unpublishable("{} is not text".format(self.file.get_name()))

    def published(self):
        ms = self.hub.get("published_ms")
        return _iso(ms if ms is not None else time.time() * 1000)

    def origin(self):
        """Topic, artifact number and observed time of a file forwarded from a broker.
        All three are None for any other file, including one whose name doesn't decode."""
        topic = artifact = observed = None
        name = self.file.get_name()
        if is_envelope(name):
            try:
                topic, artifact, observed_ms = decode_name(name)
                if observed_ms is not None:
                    observed = _iso_ms(observed_ms)
            except ValueError:
                pass
        return {"@file.origin.topic": topic, "@file.origin.artifact": artifact,
                "@file.origin.observed": observed}


# How each placeholder reads its value off one arrival. `@file.origin.*` is read in `value`.
_READERS = {
    "@node.label": lambda a: a.reception.source,
    "@node.device_id": _Arrival.device_id,
    "@node.session_id": lambda a: a.reception.session_id,
    "@node.mode": lambda a: "secure" if a.reception.device_id is not None else "open",
    "@file.name": lambda a: a.file.get_name(),
    "@file.size": lambda a: len(a.content()),
    "@file.chunks_total": lambda a: a.reception.total_chunks,
    "@file.arrival": _Arrival.arrival,
    "@file.content.text": _Arrival.text,
    "@file.content.base64": lambda a: binascii.b2a_base64(a.content()).strip().decode("ascii"),
    "@file.stats.rssi": lambda a: a.reception.rssi,
    "@file.stats.snr": lambda a: a.reception.snr,
    "@hub.site": lambda a: a.hub.get("site"),
    "@hub.published": _Arrival.published,
}


class Template:
    """One named shape, parsed once. `render` turns a finished file into message bytes."""

    def __init__(self, name, message):
        self.name = name
        self.message = message
        self._parsed = self._parse(message)

    def check_values(self, values):
        """Refuse a node's value for a key this shape doesn't have. Every message in one
        shape has the same keys, so a node may change a value but never add a key."""
        for path in values or {}:
            here = self.message
            for key in path.split("."):
                if not isinstance(here, dict) or key not in here:
                    raise ValueError("template {}: a node sets {}, which the shape does "
                                     "not have".format(self.name, path))
                here = here[key]

    def render(self, file, reception, hub, values=None):
        """The message for one finished file, as UTF-8 JSON bytes.

        `hub` holds `site` and, for a fixed publish time, `published_ms`. `values` are the
        node's own values for the shape's keys, by path ("location.lat"). They are copied in
        as they are, never read for placeholders. A known placeholder with no value for this
        file becomes null. Raises Unpublishable when the file can't take this shape."""
        self.check_values(values)
        message = self._fill(self._parsed, _Arrival(file, reception, hub))
        for path, value in (values or {}).items():
            keys = path.split(".")
            here = message
            for key in keys[:-1]:
                here = here[key]
            here[keys[-1]] = value
        return json.dumps(message).encode("utf-8")

    def _fill(self, node, arrival):
        if isinstance(node, _Text):
            return node.fill(arrival)
        if isinstance(node, dict):
            return {key: self._fill(item, arrival) for key, item in node.items()}
        if isinstance(node, list):
            return [self._fill(item, arrival) for item in node]
        return node

    def _parse(self, value):
        if isinstance(value, str):
            pieces = _split(value)
            names = [piece[0] for piece in pieces if isinstance(piece, tuple)]
            for name in names:
                if name not in PLACEHOLDERS:
                    raise ValueError("template {}: unknown placeholder {}. Known: {}".format(
                        self.name, name, ", ".join(PLACEHOLDERS)))
            return _Text(pieces) if names else "".join(pieces)
        if isinstance(value, dict):
            parsed = {}
            for key, item in value.items():
                # Keys are copied as they are. One that is exactly a placeholder is refused:
                # a consumer can't query a key that changes with every message.
                if key in PLACEHOLDERS:
                    raise ValueError("template {}: placeholder {} used as a key. Keys are "
                                     "copied as written".format(self.name, key))
                parsed[key] = self._parse(item)
            return parsed
        if isinstance(value, list):
            return [self._parse(item) for item in value]
        return value


class _Text:
    """A template string holding placeholders."""

    def __init__(self, pieces):
        self.pieces = pieces

    def fill(self, arrival):
        if len(self.pieces) == 1:
            return arrival.value(self.pieces[0][0])     # exactly a placeholder: keeps its type
        out = []
        for piece in self.pieces:
            if isinstance(piece, tuple):
                value = arrival.value(piece[0])
                if value is None:
                    return None     # a missing value inside text makes the whole field null
                out.append(str(value))
            else:
                out.append(piece)
        return "".join(out)

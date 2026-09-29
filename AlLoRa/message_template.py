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
    "@node.label", "@node.device_id", "@node.session_id", "@node.mode", "@node.lat", "@node.lng",
    "@file.name", "@file.size", "@file.chunks_total", "@file.arrival",
    "@file.content", "@file.content.text", "@file.content.base64", "@file.content.json",
    "@file.content.encoding",
    "@file.stats.rssi", "@file.stats.snr",
    "@file.origin.topic", "@file.origin.artifact", "@file.origin.observed",
    "@hub.site", "@hub.published",
)

_NAME_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789_."

# The placeholders that carry the file itself, and the encoding each one writes it in.
# `@file.content` picks per file, so it has no fixed entry.
_CONTENT = {"@file.content": None, "@file.content.text": "text",
            "@file.content.base64": "base64", "@file.content.json": "json"}

# Names whose file holds one JSON value per line. Decided by the name, not the content: an
# hourly file with one reading is also valid JSON, and it must still arrive as a list.
_LINES = (".ndjson", ".jsonl")


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

    def __init__(self, file, reception, hub, content_name=None, node=None):
        self.file = file
        self.reception = reception
        self.hub = hub or {}
        self.node = node or {}
        self.content_name = content_name    # the message's one content placeholder, if any
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

    def is_text(self):
        try:
            self.content().decode("utf-8")
            return True
        except UnicodeError:
            return False

    def either(self):
        """Text when the bytes are text, base64 otherwise: the envelope's own choice."""
        try:
            return self.content().decode("utf-8")
        except UnicodeError:
            return self.base64()

    def parsed(self):
        """The file as a JSON value, or as a list of them for a one-value-per-line file."""
        text = self.text()
        name = self.file.get_name()
        try:
            if any(name.endswith(end) for end in _LINES):
                return [json.loads(line) for line in text.split("\n") if line.strip()]
            return json.loads(text)
        except ValueError:
            raise Unpublishable("{} is not JSON".format(name))

    def base64(self):
        return binascii.b2a_base64(self.content()).strip().decode("ascii")

    def encoding(self):
        """How this message wrote the file: the fixed encoding of its content placeholder,
        or, for `@file.content`, the one it picked."""
        fixed = _CONTENT[self.content_name]
        if fixed is not None:
            return fixed
        return "text" if self.is_text() else "base64"

    def position(self, axis):
        """The node's position as placed on the site, or None for a node never placed."""
        position = self.node.get("position")
        return position.get(axis) if isinstance(position, dict) else None

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
    "@node.lat": lambda a: a.position("lat"),
    "@node.lng": lambda a: a.position("lng"),
    "@file.name": lambda a: a.file.get_name(),
    "@file.size": lambda a: len(a.content()),
    "@file.chunks_total": lambda a: a.reception.total_chunks,
    "@file.arrival": _Arrival.arrival,
    "@file.content": _Arrival.either,
    "@file.content.text": _Arrival.text,
    "@file.content.base64": _Arrival.base64,
    "@file.content.json": _Arrival.parsed,
    "@file.content.encoding": _Arrival.encoding,
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
        self._content_key = self._find_content_key()

    def _find_content_key(self):
        """The (path, placeholder) of the one content key `@file.content.encoding` describes,
        or None when the shape has no encoding. Refused unless there is exactly one content
        placeholder and it fills a whole key: otherwise the hint describes nothing clear."""
        uses = []
        _content_uses(self._parsed, "", uses)
        if not any(name == "@file.content.encoding" for _, name, _ in uses):
            return None
        content = [(path, name, exact) for path, name, exact in uses if name in _CONTENT]
        if len(content) != 1 or not content[0][2] or content[0][0] is None:
            raise ValueError("template {}: @file.content.encoding needs exactly one content "
                             "placeholder ({}) filling a whole key, and this shape has {}".format(
                                 self.name, ", ".join(_CONTENT), len(content)))
        return content[0][0], content[0][1]

    def value_keys(self):
        """The paths of this shape's fixed values ("location.lat"), in the order written.
        These are the keys a node may fill in; a key filled from the file is not one."""
        keys = []
        self._collect_values(self._parsed, "", keys)
        return keys

    def _collect_values(self, node, prefix, keys):
        for key, item in node.items():
            if isinstance(item, dict):
                self._collect_values(item, prefix + key + ".", keys)
            elif not _holds_placeholder(item):
                keys.append(prefix + key)

    def fill_keys(self):
        """The paths of keys whose whole value is one placeholder, in the order written. A node
        may switch these to another placeholder (a camera: `data` as base64), never to a fixed
        value, which would read as the file's own."""
        keys = []
        self._collect_fills(self._parsed, "", keys)
        return keys

    def _collect_fills(self, node, prefix, keys):
        for key, item in node.items():
            if isinstance(item, dict):
                self._collect_fills(item, prefix + key + ".", keys)
            elif isinstance(item, _Text) and item.exact():
                keys.append(prefix + key)

    def check_values(self, values):
        """Refuse a node's value unless it sets one of this shape's fixed values, or switches a
        key the file fills to another known placeholder. Every message in one shape has the
        same keys, so a node may never add a key or replace a group."""
        fixed = self.value_keys()
        fills = self.fill_keys()
        for path, value in (values or {}).items():
            if path in fixed:
                continue
            if path in fills:
                if not (isinstance(value, str) and value in PLACEHOLDERS):
                    raise ValueError("template {}: {} is filled by the file, so a node may "
                                     "only switch it to another placeholder, not {!r}".format(
                                         self.name, path, value))
                if self._content_key is not None and path == self._content_key[0] \
                        and value not in _CONTENT:
                    raise ValueError("template {}: {} is the content @file.content.encoding "
                                     "describes, so it switches only to {}".format(
                                         self.name, path, ", ".join(_CONTENT)))
                continue
            raise ValueError("template {}: a node sets {}, which is not one of the shape's "
                             "keys a node may set ({})".format(
                                 self.name, path, ", ".join(fixed + fills)))

    def render(self, file, reception, hub, values=None, node=None):
        """The message for one finished file, as UTF-8 JSON bytes.

        `hub` holds `site` and, for a fixed publish time, `published_ms`. `node` is the node's
        roster entry, read for its `position` ({lat, lng}). `values` are the
        node's own values by path ("location.lat"). A fixed value is copied in as it is, never
        read for placeholders; a key the file fills takes the node's placeholder instead of the
        shape's. A known placeholder with no value for this file becomes null. Raises
        Unpublishable when the file can't take this shape."""
        self.check_values(values)
        fills = self.fill_keys()
        switched = {path: _Text([(value,)]) for path, value in (values or {}).items()
                    if path in fills}
        content_name = None
        if self._content_key is not None:
            path, name = self._content_key
            content_name = values[path] if path in switched else name
        arrival = _Arrival(file, reception, hub, content_name, node)
        message = self._fill(self._parsed, arrival, switched, "")
        for path, value in (values or {}).items():
            if path in switched:
                continue
            keys = path.split(".")
            here = message
            for key in keys[:-1]:
                here = here[key]
            here[keys[-1]] = value
        return json.dumps(message).encode("utf-8")

    def _fill(self, node, arrival, switched, path):
        # Switched before filling, so the shape's own placeholder is never read: a JPEG must
        # not be refused as text when its node asked for base64.
        node = switched.get(path, node)
        if isinstance(node, _Text):
            return node.fill(arrival)
        if isinstance(node, dict):
            prefix = path + "." if path else ""
            return {key: self._fill(item, arrival, switched, prefix + key)
                    for key, item in node.items()}
        if isinstance(node, list):
            return [self._fill(item, arrival, {}, None) for item in node]   # no paths in lists
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


def _content_uses(parsed, path, uses):
    """Every placeholder in a parsed shape as (path, name, fills the whole key). Inside a
    list the path is None: a list item has no path a node could address."""
    if isinstance(parsed, _Text):
        for piece in parsed.pieces:
            if isinstance(piece, tuple):
                uses.append((path, piece[0], parsed.exact()))
    elif isinstance(parsed, dict):
        prefix = path + "." if path else ""
        for key, item in parsed.items():
            _content_uses(item, None if path is None else prefix + key, uses)
    elif isinstance(parsed, list):
        for item in parsed:
            _content_uses(item, None, uses)


def _holds_placeholder(parsed):
    if isinstance(parsed, _Text):
        return True
    if isinstance(parsed, list):
        return any(_holds_placeholder(item) for item in parsed)
    if isinstance(parsed, dict):
        return any(_holds_placeholder(item) for item in parsed.values())
    return False


class _Text:
    """A template string holding placeholders."""

    def __init__(self, pieces):
        self.pieces = pieces

    def exact(self):
        """Exactly one placeholder, nothing around it."""
        return len(self.pieces) == 1 and isinstance(self.pieces[0], tuple)

    def fill(self, arrival):
        if len(self.pieces) == 1:
            return arrival.value(self.pieces[0][0])     # exactly a placeholder: keeps its type
        out = []
        for piece in self.pieces:
            if isinstance(piece, tuple):
                value = arrival.value(piece[0])
                if value is None:
                    return None     # a missing value inside text makes the whole field null
                out.append(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
            else:
                out.append(piece)
        return "".join(out)

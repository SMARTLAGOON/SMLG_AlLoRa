"""Message templates: a deployment writes the shape of the message a finished file becomes.

A template is JSON with placeholders such as `@file.name`. It is checked once, when it is
loaded, and then rendered once per file into the bytes a sink sends.
"""
import ast
import json
import os

import pytest

from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.File import AlLoRa_File
from AlLoRa.message_template import PLACEHOLDERS, Template, Unpublishable

# 2026-09-28T10:00:03Z
ARRIVAL_MS = 1790589603000

PEDRO_V1 = {
    "tenant": "albufera",
    "device_id": "unset",
    "timestamp": "@file.arrival",
    "location": {"lat": None, "lon": None},
    "size": "@file.size",
    "rssi": "@file.stats.rssi",
    "data": "@file.content.text",
    "attachments": [],
}


def _water_reading():
    file = AlLoRa_File(name="reading_0923.json", content=b'{"t": 21.4, "h": 68.2}')
    reception = Reception(source="a1b2", session_id=7, rssi=-97, snr=8.5,
                          total_chunks=1, timestamp_ms=ARRIVAL_MS)
    return file, reception


def test_a_water_reading_fills_pedros_shape():
    file, reception = _water_reading()
    out = Template("pedro_v1", PEDRO_V1).render(file, reception, hub={"site": "albufera"})
    assert json.loads(out) == {
        "tenant": "albufera",
        "device_id": "unset",
        "timestamp": "2026-09-28T10:00:03Z",
        "location": {"lat": None, "lon": None},
        "size": 22,                                    # a number, not "22"
        "rssi": -97,
        "data": '{"t": 21.4, "h": 68.2}',
        "attachments": [],
    }


ORIGIN = {
    "source_topic": "@file.origin.topic",
    "artifact_id": "@file.origin.artifact",
    "observed_at": "@file.origin.observed",
}


def test_a_file_forwarded_from_a_broker_says_where_it_came_from():
    # How an MQTT datasource names a file: artifact 836, observed 2025-09-23T10:00:00.123Z,
    # topic "emeteo/obs" with its slash escaped.
    file = AlLoRa_File(name="mq!836!1758621600123!emeteo%2Fobs", content=b"21.4")
    reception = Reception(source="a1b2", timestamp_ms=ARRIVAL_MS)
    out = Template("origin", ORIGIN).render(file, reception, hub={"site": "albufera"})
    assert json.loads(out) == {
        "source_topic": "emeteo/obs",
        "artifact_id": 836,
        "observed_at": "2025-09-23T10:00:00.123Z",
    }


def test_a_placeholder_inside_text_becomes_text():
    file, reception = _water_reading()
    shape = {"label": "chunks: @file.chunks_total, from @node.label at @hub.site.",
             "contact": "ops@@upv.es"}
    out = Template("t", shape).render(file, reception, hub={"site": "albufera"})
    assert json.loads(out) == {"label": "chunks: 1, from a1b2 at albufera.",
                               "contact": "ops@upv.es"}


def test_a_placeholder_in_a_key_is_refused():
    with pytest.raises(ValueError) as refused:
        Template("t", {"@file.name": "x"})
    assert "@file.name" in str(refused.value)


def test_a_node_sets_its_own_values_for_the_shapes_fixed_keys():
    file, reception = _water_reading()
    out = Template("pedro_v1", PEDRO_V1).render(
        file, reception, hub={"site": "albufera"},
        values={"device_id": "meteo_001", "location.lat": 39.33, "location.lon": -0.35})
    message = json.loads(out)
    assert message["device_id"] == "meteo_001"
    assert message["location"] == {"lat": 39.33, "lon": -0.35}
    assert message["tenant"] == "albufera"          # not set by the node: the shape's value


def test_a_node_cannot_add_a_key_the_shape_does_not_have():
    template = Template("pedro_v1", PEDRO_V1)
    with pytest.raises(ValueError) as refused:
        template.check_values({"station": "W12"})
    assert "station" in str(refused.value)
    with pytest.raises(ValueError):
        template.check_values({"location.alt": 3})


def test_a_node_cannot_overwrite_what_the_file_fills_in_or_a_whole_group():
    # A hand-set timestamp would read as the file's own. A group set to one value would change
    # the keys a consumer receives, which a shape exists to keep the same.
    template = Template("pedro_v1", PEDRO_V1)
    for path, value in (("timestamp", "2020-01-01T00:00:00Z"), ("location", 3)):
        with pytest.raises(ValueError) as refused:
            template.check_values({path: value})
        assert path in str(refused.value)


def test_a_shape_names_the_keys_a_node_may_fill_in():
    # The fixed values, by path: what a website row offers for a node in this shape. Keys
    # filled from the file are not in it, and neither is a group, only what is inside it.
    template = Template("pedro_v1", PEDRO_V1)
    assert template.value_keys() == ["tenant", "device_id", "location.lat", "location.lon",
                                     "attachments"]


def test_every_placeholder_resolves():
    file = AlLoRa_File(name="mq!836!1758621600123!emeteo%2Fobs", content=b"hi")
    reception = Reception(source="a1b2", session_id=7, device_id=b"\xa1\xb2\xc3\xd4",
                          rssi=-97, snr=8.5, total_chunks=3, timestamp_ms=ARRIVAL_MS)
    shape = {name[1:]: name for name in PLACEHOLDERS}     # "node.label": "@node.label"
    out = Template("all", shape).render(
        file, reception, hub={"site": "albufera", "published_ms": ARRIVAL_MS + 2000})
    assert json.loads(out) == {
        "node.label": "a1b2",
        "node.device_id": "a1b2c3d4",
        "node.session_id": 7,
        "node.mode": "secure",
        "file.name": "mq!836!1758621600123!emeteo%2Fobs",
        "file.size": 2,
        "file.chunks_total": 3,
        "file.arrival": "2026-09-28T10:00:03Z",
        "file.content.text": "hi",
        "file.content.base64": "aGk=",
        "file.stats.rssi": -97,
        "file.stats.snr": 8.5,
        "file.origin.topic": "emeteo/obs",
        "file.origin.artifact": 836,
        "file.origin.observed": "2025-09-23T10:00:00.123Z",
        "hub.site": "albufera",
        "hub.published": "2026-09-28T10:00:05Z",
    }


def test_an_open_node_has_no_device_id():
    file, reception = _water_reading()
    shape = {"device_id": "@node.device_id", "mode": "@node.mode"}
    out = Template("t", shape).render(file, reception, hub={})
    assert json.loads(out) == {"device_id": None, "mode": "open"}


def test_a_photo_is_not_put_in_a_text_field():
    file = AlLoRa_File(name="cam.jpg", content=b"\xff\xd8\xff\xe0 not text")
    reception = Reception(source="c4m1", timestamp_ms=ARRIVAL_MS)
    with pytest.raises(Unpublishable) as refused:
        Template("pedro_v1", PEDRO_V1).render(file, reception, hub={})
    assert "cam.jpg" in str(refused.value)


def test_a_plain_file_has_the_same_keys_with_null_origin():
    file, reception = _water_reading()
    out = Template("origin", ORIGIN).render(file, reception, hub={"site": "albufera"})
    assert json.loads(out) == {"source_topic": None, "artifact_id": None, "observed_at": None}


def _imports_of(path):
    with open(path, "rb") as f:
        tree = ast.parse(f.read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_the_renderer_does_not_know_where_a_message_goes():
    # Formatting a message is not sending it. If the renderer ever needs to know the
    # destination, the design is wrong: that belongs in the sink.
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "AlLoRa", "message_template.py")
    banned = ("AlLoRa.DataSinks", "socket", "usocket", "requests", "urequests",
              "umqtt", "paho", "http", "urllib", "ssl")
    offenders = [name for name in _imports_of(path)
                 if any(name == b or name.startswith(b + ".") for b in banned)]
    assert offenders == []


def test_a_typo_in_a_placeholder_is_refused_when_the_template_is_loaded():
    with pytest.raises(ValueError) as refused:
        Template("pedro_v1", {"rssi": "@file.rsi"})
    message = str(refused.value)
    assert "@file.rsi" in message
    assert "pedro_v1" in message
    assert "@file.stats.rssi" in message     # the valid names are listed

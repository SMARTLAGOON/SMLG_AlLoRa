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


def test_a_camera_switches_the_data_placeholder_and_keeps_the_shape():
    # One shape for the sensor nodes and the camera: the camera's JPEG can't be text, so its
    # node switches `data` to base64. Switched before filling, so the text placeholder is never
    # tried on the photo.
    file = AlLoRa_File(name="boat_0001.jpg", content=b"\xff\xd8\xff\xe0")
    reception = Reception(source="c3d4", timestamp_ms=ARRIVAL_MS)
    out = Template("pedro_v1", PEDRO_V1).render(
        file, reception, hub={"site": "albufera"},
        values={"data": "@file.content.base64", "device_id": "boat_cam"})
    message = json.loads(out)
    assert message["data"] == "/9j/4A=="
    assert message["device_id"] == "boat_cam"
    assert set(message) == set(PEDRO_V1)


def test_a_switched_placeholder_keeps_its_type():
    file, reception = _water_reading()
    out = Template("t", {"n": "@file.size"}).render(
        file, reception, hub={}, values={"n": "@file.chunks_total"})
    assert json.loads(out) == {"n": 1}


def test_a_key_the_file_fills_takes_only_a_known_placeholder():
    template = Template("pedro_v1", PEDRO_V1)
    for value in ("2020-01-01T00:00:00Z", "@file.contents.text", "at @file.arrival", 3, None):
        with pytest.raises(ValueError) as refused:
            template.check_values({"timestamp": value})
        assert "timestamp" in str(refused.value)


def test_a_fixed_value_stays_literal_even_when_it_looks_like_a_placeholder():
    file, reception = _water_reading()
    out = Template("pedro_v1", PEDRO_V1).render(
        file, reception, hub={}, values={"device_id": "@node.device_id"})
    assert json.loads(out)["device_id"] == "@node.device_id"


def test_a_shape_names_the_keys_a_node_may_switch():
    # Keys whose whole value is one placeholder. Text around a placeholder can't be switched,
    # and nothing inside a list is addressable by path.
    template = Template("t", {"when": "@file.arrival", "label": "from @node.label",
                              "at": {"rssi": "@file.stats.rssi"}, "list": ["@file.name"],
                              "tenant": "albufera"})
    assert template.fill_keys() == ["when", "at.rssi"]
    with pytest.raises(ValueError):
        template.check_values({"label": "@node.mode"})


def test_every_placeholder_resolves():
    file = AlLoRa_File(name="mq!836!1758621600123!emeteo%2Fobs", content=b"hi")
    reception = Reception(source="a1b2", session_id=7, device_id=b"\xa1\xb2\xc3\xd4",
                          rssi=-97, snr=8.5, total_chunks=3, timestamp_ms=ARRIVAL_MS)
    # "node.label": "@node.label". The encoding needs a single content key, and JSON content
    # needs a JSON file, so each has its own tests.
    shape = {name[1:]: name for name in PLACEHOLDERS
             if name not in ("@file.content.encoding", "@file.content.json")}
    out = Template("all", shape).render(
        file, reception, hub={"site": "albufera", "published_ms": ARRIVAL_MS + 2000},
        node={"position": {"lat": 39.33, "lng": -0.35, "source": "dragged"}})
    assert json.loads(out) == {
        "node.label": "a1b2",
        "node.device_id": "a1b2c3d4",
        "node.session_id": 7,
        "node.mode": "secure",
        "node.lat": 39.33,
        "node.lng": -0.35,
        "file.name": "mq!836!1758621600123!emeteo%2Fobs",
        "file.size": 2,
        "file.chunks_total": 3,
        "file.arrival": "2026-09-28T10:00:03Z",
        "file.content": "hi",
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


PLACED = {"location": {"lat": "@node.lat", "lon": "@node.lng"}}


def test_a_node_sends_its_position_on_the_map():
    file, reception = _water_reading()
    out = Template("t", PLACED).render(file, reception, hub={},
                                       node={"position": {"lat": 39.33, "lng": -0.35,
                                                          "source": "typed"}})
    assert json.loads(out) == {"location": {"lat": 39.33, "lon": -0.35}}


def test_a_node_never_placed_has_a_null_position():
    # No fallback: a made-up position reads as a real one.
    file, reception = _water_reading()
    # A position mangled by hand in Nodes.json reads as none, not as a node that stops publishing.
    for node in (None, {}, {"position": None}, {"position": [39.33, -0.35]}):
        out = Template("t", PLACED).render(file, reception, hub={}, node=node)
        assert json.loads(out) == {"location": {"lat": None, "lon": None}}


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


SAYS_HOW = {"data": "@file.content", "encoding": "@file.content.encoding"}


def test_content_is_text_when_it_can_be_and_says_so():
    file, reception = _water_reading()
    out = Template("t", SAYS_HOW).render(file, reception, hub={})
    assert json.loads(out) == {"data": '{"t": 21.4, "h": 68.2}', "encoding": "text"}


def test_a_photo_in_the_same_shape_is_base64_and_says_so():
    # The camera needs no switch: the shape picks, and the hint names the pick.
    file = AlLoRa_File(name="cam.jpg", content=b"\xff\xd8\xff\xe0")
    reception = Reception(source="c4m1", timestamp_ms=ARRIVAL_MS)
    out = Template("t", SAYS_HOW).render(file, reception, hub={})
    assert json.loads(out) == {"data": "/9j/4A==", "encoding": "base64"}


def test_the_encoding_follows_a_node_that_switches_its_content():
    # A text file forced to base64 must not arrive labelled "text".
    file, reception = _water_reading()
    for switched, named in (("@file.content.base64", "base64"),
                            ("@file.content.text", "text")):
        out = Template("t", SAYS_HOW).render(file, reception, hub={},
                                             values={"data": switched})
        assert json.loads(out)["encoding"] == named


def test_the_key_an_encoding_describes_switches_only_to_other_content():
    template = Template("t", SAYS_HOW)
    with pytest.raises(ValueError) as refused:
        template.check_values({"data": "@file.name"})
    assert "data" in str(refused.value)


def test_an_encoding_with_no_single_content_key_is_refused_when_loaded():
    # With no content, or two, or content inside text, the hint would describe nothing clear.
    for shape in ({"encoding": "@file.content.encoding"},
                  {"a": "@file.content", "b": "@file.content.base64",
                   "encoding": "@file.content.encoding"},
                  {"a": "got @file.content", "encoding": "@file.content.encoding"}):
        with pytest.raises(ValueError) as refused:
            Template("t", shape)
        assert "@file.content.encoding" in str(refused.value)


AS_JSON = {"data": "@file.content.json", "encoding": "@file.content.encoding"}


def test_a_json_file_arrives_as_an_object_a_consumer_can_query():
    file = AlLoRa_File(name="boat_0042.json",
                       content=b'{"boats": 2, "image": "/9j/4A==", "centroids": [[3, 4]]}')
    reception = Reception(source="c4m1", timestamp_ms=ARRIVAL_MS)
    out = Template("t", AS_JSON).render(file, reception, hub={})
    assert json.loads(out) == {"data": {"boats": 2, "image": "/9j/4A==",
                                        "centroids": [[3, 4]]},
                               "encoding": "json"}


def test_an_ndjson_file_arrives_as_a_list_of_readings():
    file = AlLoRa_File(name="uptime_hour_0000.ndjson",
                       content=b'{"t": 21.4}\n{"t": 21.6}\n\n{"t": 21.9}\n')
    reception = Reception(source="a1b2", timestamp_ms=ARRIVAL_MS)
    out = Template("t", AS_JSON).render(file, reception, hub={})
    assert json.loads(out)["data"] == [{"t": 21.4}, {"t": 21.6}, {"t": 21.9}]


def test_an_ndjson_file_with_one_reading_is_still_a_list():
    # The name decides, not the content: one line is also valid JSON, and a consumer must not
    # get an object one hour and a list the next.
    for name in ("uptime_hour_0001.ndjson", "log.jsonl"):
        file = AlLoRa_File(name=name, content=b'{"t": 21.4}\n')
        reception = Reception(source="a1b2", timestamp_ms=ARRIVAL_MS)
        out = Template("t", AS_JSON).render(file, reception, hub={})
        assert json.loads(out)["data"] == [{"t": 21.4}]


def test_a_file_that_is_not_json_is_not_published():
    reception = Reception(source="a1b2", timestamp_ms=ARRIVAL_MS)
    for name, content in (("cam.jpg", b"\xff\xd8\xff\xe0"),
                          ("notes.txt", b"t=21.4"),
                          ("readings.json", b'{"t": 21.4}\n{"t": 21.6}\n'),
                          ("broken.ndjson", b'{"t": 21.4}\nnot json\n')):
        with pytest.raises(Unpublishable) as refused:
            Template("t", AS_JSON).render(AlLoRa_File(name=name, content=content),
                                          reception, hub={})
        assert name in str(refused.value)


def test_a_node_switches_its_content_to_json_and_the_encoding_says_so():
    file, reception = _water_reading()
    out = Template("t", SAYS_HOW).render(file, reception, hub={},
                                         values={"data": "@file.content.json"})
    assert json.loads(out) == {"data": {"t": 21.4, "h": 68.2}, "encoding": "json"}


def test_json_content_inside_text_is_written_as_json_text():
    file, reception = _water_reading()
    out = Template("t", {"note": "got @file.content.json"}).render(file, reception, hub={})
    assert json.loads(json.loads(out)["note"][4:]) == {"t": 21.4, "h": 68.2}


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

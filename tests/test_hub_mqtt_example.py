"""The MQTT gateway example: a Hub whose received files go to a broker instead of to disk.

`examples/Hubs/Many-Edges/USB/main_mqtt.py` is the one example that exercises the DataSink seam
in a deployment shape rather than in a unit test. It had no coverage at all until now, which is
how it could keep claiming a topic layout the sink no longer produced, or import a class that had
moved, and nobody would find out until a gateway was standing on a bench.

Two things are pinned here, and deliberately only two. That the example still loads, which is
what a moved import or a renamed helper breaks. And that the topic it documents in its header is
the topic the sink actually publishes on, because that string is what a deployment subscribes to
and it is written down in two places.
"""
import importlib.util
import os
import sys

import pytest

from AlLoRa.DataSinks.DataSink import Reception
from AlLoRa.File import AlLoRa_File

_USB_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "Hubs", "Many-Edges", "USB")


def _load_example():
    """Import the example without running it. The folder goes on sys.path because the example
    does `from main import ...`, exactly as it resolves when a host runs `python3 main_mqtt.py`
    in that directory. `main` is a generic enough name that any copy already in sys.modules has
    to be moved aside, or this would silently bind to someone else's."""
    saved_path = list(sys.path)
    saved_main = sys.modules.pop("main", None)
    sys.path.insert(0, _USB_EXAMPLE)
    try:
        spec = importlib.util.spec_from_file_location(
            "usb_hub_main_mqtt", os.path.join(_USB_EXAMPLE, "main_mqtt.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = saved_path
        sys.modules.pop("main", None)
        if saved_main is not None:
            sys.modules["main"] = saved_main


class _Recording_client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload))


def test_the_example_still_loads():
    # Its imports are the coverage: MQTT_DataSink, Serial_connector, and the two adapter
    # helpers it shares with the disk example next door rather than copying.
    module = _load_example()
    assert callable(module.find_adapter)
    assert callable(module.reset_adapter)


def test_the_documented_topic_is_the_one_the_sink_publishes():
    # The header promises <topic_prefix>/<source>/<filename> with the payload verbatim. A
    # deployment subscribes to that string, so a change to either side has to break here rather
    # than on a gateway.
    module = _load_example()
    from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink

    client = _Recording_client()
    sink = MQTT_DataSink(client=client, topic_prefix=module.TOPIC_PREFIX)
    payload = bytes(range(64))
    sink.consume(AlLoRa_File(name="reading.json", content=bytearray(payload)),
                 Reception(source="a1a1a1a1"))

    assert client.published == [("{}/a1a1a1a1/reading.json".format(module.TOPIC_PREFIX), payload)]

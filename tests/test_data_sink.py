"""DataSink — the Collector's completed-file output boundary (symmetric to DataSource).

v3 splits "what to do with a received file" out of the Collector's hardwired disk save. A
Collector (Requester/Gateway) hands every finished AlLoRa_File to its DataSink.consume(file,
source). The default Disk_DataSink reproduces the legacy Results/<source>/<name> save
byte-for-byte, so nothing that relies on files-on-disk changes; an MQTT_DataSink publishes the
file to a broker instead, without the Collector knowing where its data goes.

This proves: (1) the default path is unchanged (files still land on disk), (2) an injected sink
receives the completed file end-to-end over a real transfer and replaces the disk write, and
(3) the Disk / MQTT sinks and File.discard behave in isolation.
"""
import json
import math
import os
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Source import Source
from AlLoRa.Nodes.Requester import Requester
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File
from AlLoRa.DataSinks.DataSink import DataSink, Reception
from AlLoRa.DataSinks.Disk_DataSink import Disk_DataSink
from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "sink-v3", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _run_transfer(tmp_path, payload, filename, data_sink=None):
    """A full v3-open Source -> Collector transfer over the loopback seam, optionally with a
    custom sink on the Collector. Mirrors test_v3_open_transfer's harness."""
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    source = Source(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename, content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))
    collector = Requester(collector_conn, config_file=config_file, data_sink=data_sink)
    endpoint = Digital_Endpoint(name="src", mac_address=SOURCE_MAC,
                                active=True, session_id=SESSION_ID)

    errors = []

    def serve():
        try:
            source.send_file(timeout=30)
        except Exception as e:  # pragma: no cover - surfaced via the assert below
            errors.append(e)

    server = threading.Thread(target=serve, name="source-serve-sink", daemon=True)
    server.start()

    collector.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
    server.join(timeout=15)

    assert not errors, "source thread raised: {}".format(errors)
    assert not server.is_alive(), "source did not finish serving the file"


def _received_file(base_path, name, payload, chunk_size=8):
    """Build a fully-reassembled *received-style* AlLoRa_File (the assembly path), as a Collector
    holds one the instant before it hands it to a sink."""
    num_chunks = math.ceil(len(payload) / chunk_size)
    f = AlLoRa_File(name=name, length=num_chunks, chunk_size=chunk_size, path=base_path)
    for i in range(num_chunks):
        f.add_chunk(i, payload[i * chunk_size:(i + 1) * chunk_size])
    return f


class _CapturingSink(DataSink):
    def __init__(self):
        self.received = []   # (reception, name, bytes)

    def consume(self, file, reception=None):
        self.received.append((reception, file.get_name(), bytes(file.get_content())))


class _FakeMQTTClient:
    def __init__(self):
        self.published = []   # (topic, bytes)

    def publish(self, topic, msg, *args, **kwargs):
        self.published.append((topic, bytes(msg)))


# --- end-to-end: the sink seam in a real transfer -------------------------------------------

def test_default_sink_preserves_legacy_disk_save(tmp_path):
    payload = bytes(i % 256 for i in range(1000))   # 5 chunks at 243
    _run_transfer(tmp_path, payload, "v3.bin")       # no sink -> default Disk_DataSink
    saved = tmp_path / "Results" / SOURCE_MAC / "v3.bin"
    assert saved.exists(), "default sink no longer saves to Results/<mac>/<name>"
    assert saved.read_bytes() == payload, "default disk save is no longer byte-identical"


def test_injected_sink_receives_completed_file_and_replaces_disk(tmp_path):
    payload = bytes(i % 256 for i in range(1000))
    sink = _CapturingSink()
    _run_transfer(tmp_path, payload, "v3.bin", data_sink=sink)

    assert len(sink.received) == 1, "sink was not handed exactly one completed file"
    rec, name, content = sink.received[0]
    assert rec.source == SOURCE_MAC
    assert rec.session_id == SESSION_ID, "completion context did not carry the session id"
    assert name == "v3.bin"
    assert content == payload, "sink received a corrupt/incomplete file"
    # A custom sink replaces the disk write; the file must not also be persisted.
    assert not (tmp_path / "Results" / SOURCE_MAC / "v3.bin").exists()


# --- unit: the concrete sinks + File.discard ------------------------------------------------

def test_disk_sink_writes_result_path_source_subfolder(tmp_path):
    payload = b"hello disk sink, arbitrary bytes"
    # As in the real flow, a received file reassembles under result_path/<source> (the same folder
    # the disk sink then finalizes it into); AlLoRa's mkdir is non-recursive, so result_path exists.
    result_path = str(tmp_path / "Out")
    os.makedirs(result_path, exist_ok=True)
    f = _received_file(result_path + "/" + SOURCE_MAC, "d.bin", payload, chunk_size=4)
    Disk_DataSink(result_path).consume(f, Reception(source=SOURCE_MAC))
    saved = tmp_path / "Out" / SOURCE_MAC / "d.bin"
    assert saved.exists()
    assert saved.read_bytes() == payload


def test_mqtt_sink_publishes_file_bytes_on_derived_topic(tmp_path):
    payload = b"telemetry-json-or-image-bytes"
    f = _received_file(str(tmp_path / "recv"), "cam.jpg", payload, chunk_size=8)
    fake = _FakeMQTTClient()
    MQTT_DataSink(topic_prefix="allora", client=fake).consume(f, Reception(source=SOURCE_MAC))

    assert len(fake.published) == 1
    topic, msg = fake.published[0]
    assert topic == "allora/{}/cam.jpg".format(SOURCE_MAC)
    assert msg == payload


def test_mqtt_sink_custom_topic_builder(tmp_path):
    payload = b"metric-payload"
    f = _received_file(str(tmp_path / "recv"), "temp.json", payload, chunk_size=8)
    fake = _FakeMQTTClient()
    sink = MQTT_DataSink(client=fake,
                         topic_for=lambda source, name: "allora/upv/{}/metrics".format(source))
    sink.consume(f, Reception(source=SOURCE_MAC))

    assert fake.published[0][0] == "allora/upv/{}/metrics".format(SOURCE_MAC)
    assert fake.published[0][1] == payload


def test_mqtt_sink_cleans_up_reassembly_temp_by_default(tmp_path):
    payload = b"published then discarded"
    f = _received_file(str(tmp_path / "recv"), "x.bin", payload, chunk_size=4)
    temp = f.temp_file_path
    assert os.path.exists(temp)
    MQTT_DataSink(client=_FakeMQTTClient()).consume(f, Reception(source=SOURCE_MAC))
    assert not os.path.exists(temp), "MQTT sink left the reassembly temp behind"


def test_file_discard_releases_temp_without_persisting(tmp_path):
    payload = b"discard me"
    f = _received_file(str(tmp_path / "recv"), "x.bin", payload, chunk_size=4)
    assert f.get_content() == payload
    temp = f.temp_file_path
    assert os.path.exists(temp)
    f.discard()
    assert not os.path.exists(temp)
    # No final file was written anywhere under the receive folder.
    assert not os.path.exists(os.path.join(str(tmp_path / "recv"), "x.bin"))


def test_base_datasink_consume_is_abstract():
    try:
        DataSink().consume(None, None)
    except NotImplementedError:
        return
    raise AssertionError("DataSink.consume must be overridden, not silently no-op")


def test_mqtt_sink_republishes_envelope_files_on_their_original_topic(tmp_path):
    # The paired-bridge case: an MQTT_Datasource on the far side named the file with the
    # envelope; the sink must republish on the embedded topic, payload byte-for-byte.
    from AlLoRa.DataSources.mqtt_naming import encode_name
    payload = b"21.5"
    name = encode_name("sensors/greenhouse/temp", 7, 123)
    f = _received_file(str(tmp_path / "recv"), name, payload, chunk_size=8)
    fake = _FakeMQTTClient()
    MQTT_DataSink(topic_prefix="allora", client=fake).consume(f, Reception(source=SOURCE_MAC))
    assert fake.published[0] == ("sensors/greenhouse/temp", payload)


def test_mqtt_sink_explicit_topic_for_still_wins_over_the_envelope(tmp_path):
    # Precedence: explicit override > pairing convention > derived default. A topic_for
    # deployment keeps full control (it can decode the envelope itself if it wants it).
    from AlLoRa.DataSources.mqtt_naming import encode_name
    f = _received_file(str(tmp_path / "recv"),
                       encode_name("sensors/t", 1), b"x", chunk_size=8)
    fake = _FakeMQTTClient()
    sink = MQTT_DataSink(client=fake, topic_for=lambda source, name: "rerouted/all")
    sink.consume(f, Reception(source=SOURCE_MAC))
    assert fake.published[0][0] == "rerouted/all"


def test_mqtt_sink_falls_back_to_derived_topic_for_malformed_envelope(tmp_path):
    # A plain file that merely starts with the prefix must not publish to a garbage
    # topic — it takes the normal <prefix>/<source>/<filename> route.
    f = _received_file(str(tmp_path / "recv"), "mq!oops", b"x", chunk_size=8)
    fake = _FakeMQTTClient()
    MQTT_DataSink(topic_prefix="allora", client=fake).consume(f, Reception(source=SOURCE_MAC))
    assert fake.published[0][0] == "allora/{}/mq!oops".format(SOURCE_MAC)


def test_mqtt_sink_notes_published_messages_on_the_loop_guard(tmp_path):
    # The other half of loop prevention: whatever the sink republishes is remembered, so
    # the co-located datasource drops the echo instead of shipping it back.
    from AlLoRa.DataSources.Loop_guard import Loop_guard
    from AlLoRa.DataSources.mqtt_naming import encode_name
    guard = Loop_guard()
    payload = b"21.5"
    f = _received_file(str(tmp_path / "recv"), encode_name("sensors/t", 3), payload, chunk_size=8)
    sink = MQTT_DataSink(client=_FakeMQTTClient(), loop_guard=guard)
    sink.consume(f, Reception(source=SOURCE_MAC))
    assert guard.seen("sensors/t", payload)


class _RecordingClient:
    def __init__(self):
        self.calls = []   # (args, kwargs)

    def publish(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_publish_uses_paho_signature_for_paho_flavor():
    # paho: publish(topic, payload, qos=, retain=). Guarded here so the arg shape is checked on
    # CPython instead of only surfacing against a real broker.
    sink = MQTT_DataSink(client=_RecordingClient(), qos=1, retain=True)
    sink._flavor = "paho"
    sink._publish("allora/a/f.bin", b"payload")
    (args, kwargs) = sink._client.calls[0]
    assert args == ("allora/a/f.bin", b"payload")
    assert kwargs == {"qos": 1, "retain": True}


def test_publish_uses_umqtt_positional_signature_and_bytes():
    # umqtt.simple/robust: publish(topic, msg, retain, qos) — positional, bytes topic + payload.
    sink = MQTT_DataSink(client=_RecordingClient(), qos=1, retain=True)
    sink._flavor = "umqtt"
    sink._publish("allora/a/f.bin", b"payload")
    (args, kwargs) = sink._client.calls[0]
    assert args == (b"allora/a/f.bin", b"payload", True, 1)
    assert kwargs == {}

"""DataSink — the Collector's completed-file output boundary (symmetric to DataSource).

v3 splits "what to do with a received file" out of the Collector's hardwired disk save. A
Collector (Requester/Gateway) hands every finished AlLoRa_File to its DataSink.consume(file,
source). The default Disk_DataSink reproduces the legacy Results/<source>/<name> save
byte-for-byte, so nothing that relies on files-on-disk changes; an MQTT_DataSink publishes the
file to a broker instead, and an HTTP_DataSink posts it to a web service, without the Collector
knowing where its data goes.

This proves: (1) the default path is unchanged (files still land on disk), (2) an injected sink
receives the completed file end-to-end over a real transfer and replaces the disk write, and
(3) the Disk / MQTT / HTTP sinks and File.discard behave in isolation.

The HTTP sink carries one property the other two do not have to think about: it talks to
something that can refuse. A broker publish at QoS 0 and a disk write effectively cannot fail
halfway, but a site can be down, full or wrong about its token. So the tests below pin what
happens on a refusal as hard as what happens on success, because the failure mode of getting
that wrong is silent data loss after the radio has already spent the minutes.
"""
import json
import math
import os
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Nodes.Hub import Hub
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.File import AlLoRa_File
from AlLoRa.DataSinks.DataSink import DataSink, Reception
from AlLoRa.DataSinks.Disk_DataSink import Disk_DataSink
from AlLoRa.DataSinks.HTTP_DataSink import HTTP_DataSink
from AlLoRa.DataSinks.MQTT_DataSink import MQTT_DataSink

SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
SESSION_ID = 42


def _write_config(path, result_path):
    config = {
        "name": "sink-v3", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": SESSION_ID,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _run_transfer(tmp_path, payload, filename, data_sink=None, endpoint=None):
    """A full v3-open Source -> Collector transfer over the loopback seam, optionally with a
    custom sink on the Collector. Mirrors test_v3_open_transfer's harness."""
    result_path = str(tmp_path / "Results")
    config_file = str(tmp_path / "LoRa.json")
    _write_config(config_file, result_path)

    source_conn, collector_conn = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    source = Edge(source_conn, config_file=config_file)
    source.set_file(AlLoRa_File(name=filename, content=bytearray(payload),
                                chunk_size=source.get_chunk_size()))
    collector = Hub(collector_conn, config_file=config_file, data_sink=data_sink)
    if endpoint is None:
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


class _FakeResponse:
    def __init__(self, status_code, client):
        self.status_code = status_code
        self._client = client

    def close(self):
        self._client.closed += 1


class _FakeHTTPClient:
    """Stand-in for `requests` / `urequests`, which the sink treats as the client itself: both
    libraries are modules exposing the one call it makes, so a test double is any object with
    the same `post`."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []       # (url, body bytes, headers, other kwargs)
        self.closed = 0

    def post(self, url, data=None, headers=None, **kwargs):
        self.calls.append((url, bytes(data), dict(headers or {}), kwargs))
        return _FakeResponse(self.status_code, self)


SITE_URL = "https://control.example/api/ingest"


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


def test_a_registered_endpoints_file_lands_under_its_identity_not_00000000(tmp_path):
    # A device_id-registered endpoint has no MAC, so `mac_address` stays at its "00000000"
    # default and the folder was built from that: every registered Edge saved into
    # Results/00000000/, where same-named files silently overwrote each other. A
    # single-endpoint test cannot see it; the deployment it breaks is a Hub with several
    # registered nodes, which is what secure mode is for.
    payload = bytes(i % 256 for i in range(1000))
    endpoint = Digital_Endpoint(name="src", active=True, session_id=SESSION_ID,
                                device_id="1ee385e641d52898172380d95b7914ec")
    sink = _CapturingSink()
    _run_transfer(tmp_path, payload, "v3.bin", data_sink=sink, endpoint=endpoint)

    assert endpoint.get_mac_address() == "00000000", "no MAC is the premise of this test"
    assert len(sink.received) == 1, "sink was not handed exactly one completed file"
    rec, _, content = sink.received[0]
    assert rec.source == "1ee385e6", "the sink's folder/topic key is still the absent MAC"
    assert content == payload


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
    # The paired-bridge case: an MQTT_DataSource on the far side named the file with the
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


# --- HTTP sink: the last leg, and the only sink talking to something that can refuse ---------

def test_http_sink_posts_the_file_bytes_as_the_request_body(tmp_path):
    """Raw bytes, not multipart and not base64 in JSON. A radio spent minutes on this payload;
    inflating it by a third to get it through a JSON string would be paid for twice, once in
    bandwidth and once in a heap that has no room for the second copy."""
    payload = b"telemetry-json-or-image-bytes"
    f = _received_file(str(tmp_path / "recv"), "cam.jpg", payload, chunk_size=8)
    client = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, client=client).consume(f, Reception(source=SOURCE_MAC))

    assert len(client.calls) == 1
    url, body, headers, _ = client.calls[0]
    assert url == SITE_URL
    assert body == payload
    assert headers["Content-Type"] == "application/octet-stream"
    assert headers["X-AlLoRa-Filename"] == "cam.jpg"


def test_http_sink_carries_the_reception_snapshot_in_headers(tmp_path):
    """Everything the receiving site needs to file the row: who sent it, over what session and
    identity, and how the link was behaving when it landed."""
    f = _received_file(str(tmp_path / "recv"), "t.json", b"21.5", chunk_size=4)
    client = _FakeHTTPClient()
    reception = Reception(source=SOURCE_MAC, session_id=SESSION_ID, device_id="ab12cd34",
                          rssi=-97, snr=7, total_chunks=1, timestamp_ms=1757100000000)
    HTTP_DataSink(url=SITE_URL, client=client).consume(f, reception)

    headers = client.calls[0][2]
    assert headers["X-AlLoRa-Source"] == SOURCE_MAC
    assert headers["X-AlLoRa-Session-Id"] == str(SESSION_ID)
    assert headers["X-AlLoRa-Device-Id"] == "ab12cd34"
    assert headers["X-AlLoRa-Rssi"] == "-97"
    assert headers["X-AlLoRa-Snr"] == "7"
    assert headers["X-AlLoRa-Total-Chunks"] == "1"
    assert headers["X-AlLoRa-Timestamp-Ms"] == "1757100000000"


def test_http_sink_omits_a_header_the_transfer_had_no_value_for(tmp_path):
    """An open-mode node has no device_id. The header must be absent rather than carry the
    string "None", which a database would happily store as an identity."""
    f = _received_file(str(tmp_path / "recv"), "t.json", b"21.5", chunk_size=4)
    client = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, client=client).consume(
        f, Reception(source=SOURCE_MAC, session_id=SESSION_ID))

    headers = client.calls[0][2]
    assert "X-AlLoRa-Device-Id" not in headers
    assert "X-AlLoRa-Rssi" not in headers
    assert "None" not in "".join(headers.values())


def test_http_sink_sends_a_bearer_token_only_when_one_is_configured(tmp_path):
    f = _received_file(str(tmp_path / "recv"), "a.bin", b"x", chunk_size=4)
    client = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, token="s3cret", client=client).consume(f, None)
    assert client.calls[0][2]["Authorization"] == "Bearer s3cret"

    g = _received_file(str(tmp_path / "recv2"), "b.bin", b"x", chunk_size=4)
    plain = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, client=plain).consume(g, None)
    assert "Authorization" not in plain.calls[0][2]


def test_http_sink_raises_and_keeps_the_temp_when_the_site_refuses(tmp_path):
    """The property this sink exists to get right. Node's consume call site treats an exception
    as "not delivered": it discards the temp itself, rewinds the endpoint and re-pulls the file
    next round. So a refusal must raise and must not have already thrown the bytes away, or a
    site outage becomes silent data loss after the radio has paid for the transfer."""
    payload = b"must survive a 503"
    f = _received_file(str(tmp_path / "recv"), "x.bin", payload, chunk_size=4)
    temp = f.temp_file_path
    sink = HTTP_DataSink(url=SITE_URL, client=_FakeHTTPClient(status_code=503))

    try:
        sink.consume(f, Reception(source=SOURCE_MAC))
    except Exception as e:
        assert "503" in str(e), "the refusal must say what the site answered"
    else:
        raise AssertionError("a refused post must raise, not return quietly")

    assert os.path.exists(temp), "a refused post threw the reassembly temp away"


def test_http_sink_cleans_up_the_temp_only_after_a_confirmed_post(tmp_path):
    payload = b"posted then discarded"
    f = _received_file(str(tmp_path / "recv"), "x.bin", payload, chunk_size=4)
    temp = f.temp_file_path
    assert os.path.exists(temp)
    HTTP_DataSink(url=SITE_URL, client=_FakeHTTPClient()).consume(f, Reception(source=SOURCE_MAC))
    assert not os.path.exists(temp), "HTTP sink left the reassembly temp behind"


def test_http_sink_keeps_the_temp_when_cleanup_is_off(tmp_path):
    f = _received_file(str(tmp_path / "recv"), "x.bin", b"keep me", chunk_size=4)
    temp = f.temp_file_path
    HTTP_DataSink(url=SITE_URL, cleanup=False, client=_FakeHTTPClient()).consume(f, None)
    assert os.path.exists(temp)


def test_http_sink_without_a_url_is_refused_at_construction(tmp_path):
    """MQTT_DataSink may default its host, because a Hub beside its broker is the ordinary
    deployment and localhost is usually the right guess. There is no right guess for a site."""
    try:
        HTTP_DataSink()
    except ValueError as e:
        assert "url" in str(e)
        return
    raise AssertionError("a sink with nowhere to post must not be constructible")


def test_http_sink_closes_every_response(tmp_path):
    """urequests holds the socket until the response is closed. A Hub leaking one per file runs
    out within a day of ordinary collection, and does it on the board rather than in a test."""
    client = _FakeHTTPClient()
    sink = HTTP_DataSink(url=SITE_URL, client=client)
    for i in range(3):
        f = _received_file(str(tmp_path / "recv{}".format(i)), "x.bin", b"y", chunk_size=4)
        sink.consume(f, None)
    assert client.closed == 3

    # And on the failure path too, which is where a leak would otherwise accumulate fastest.
    refusing = _FakeHTTPClient(status_code=500)
    bad = HTTP_DataSink(url=SITE_URL, client=refusing)
    g = _received_file(str(tmp_path / "recvbad"), "x.bin", b"y", chunk_size=4)
    try:
        bad.consume(g, None)
    except Exception:
        pass
    assert refusing.closed == 1


def test_http_sink_omits_timeout_when_it_is_none(tmp_path):
    """Older urequests builds take no `timeout`. Rather than a second sink for those boards,
    the config sets it to null and the keyword is left off the call."""
    f = _received_file(str(tmp_path / "recv"), "x.bin", b"y", chunk_size=4)
    client = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, timeout=None, client=client).consume(f, None)
    assert "timeout" not in client.calls[0][3]

    g = _received_file(str(tmp_path / "recv2"), "x.bin", b"y", chunk_size=4)
    timed = _FakeHTTPClient()
    HTTP_DataSink(url=SITE_URL, timeout=30, client=timed).consume(g, None)
    assert timed.calls[0][3]["timeout"] == 30


def test_http_sink_receives_a_completed_file_over_a_real_transfer(tmp_path):
    """End to end over the loopback seam, the same proof the injected sink gets: the file that
    reaches the site is the file the Edge served, whole."""
    payload = bytes(i % 256 for i in range(1000))   # 5 chunks at 243
    client = _FakeHTTPClient()
    _run_transfer(tmp_path, payload, "v3.bin",
                  data_sink=HTTP_DataSink(url=SITE_URL, token="t", client=client))

    assert len(client.calls) == 1, "the site was not posted exactly one completed file"
    url, body, headers, _ = client.calls[0]
    assert url == SITE_URL
    assert body == payload, "the site received a corrupt or incomplete file"
    assert headers["X-AlLoRa-Filename"] == "v3.bin"
    assert headers["X-AlLoRa-Source"] == SOURCE_MAC
    assert headers["X-AlLoRa-Session-Id"] == str(SESSION_ID)
    # The HTTP sink replaces the disk write, like any other sink.
    assert not (tmp_path / "Results" / SOURCE_MAC / "v3.bin").exists()

"""MQTT_DataSource — the ingest half of the MQTT bridge (MQTT PUBLISH -> AlLoRa_File).

Subscribes to a broker and turns each matching PUBLISH into an envelope-named AlLoRa_File
on the queue, ready for whoever holds the source role to serve. The client is injected
and duck-typed (umqtt-shaped: set_callback / subscribe / check_msg), so tests and CI run
broker-free; check() is the non-blocking pump the node loop calls — it never blocks the
radio loop.
"""

from AlLoRa.DataSources.MQTT_DataSource import MQTT_DataSource
from AlLoRa.DataSources.mqtt_naming import decode_name


class Fake_client:
    """An umqtt-shaped in-memory broker client: deliveries queue in `pending` and reach
    the registered callback one per check_msg(), like umqtt's non-blocking pump."""

    def __init__(self):
        self.cb = None
        self.subscribed = []
        self.pending = []
        self.disconnected = False

    def set_callback(self, cb):
        self.cb = cb

    def subscribe(self, topic):
        self.subscribed.append(topic)

    def check_msg(self):
        if self.pending:
            topic, payload = self.pending.pop(0)
            self.cb(topic, payload)

    def disconnect(self):
        self.disconnected = True


def _source(**kwargs):
    client = Fake_client()
    ds = MQTT_DataSource(file_chunk_size=200, topics=("sensors/#",),
                         client=client, **kwargs)
    ds.prepare()
    return ds, client


def test_prepare_subscribes_the_configured_topics():
    ds, client = _source()
    assert client.subscribed == ["sensors/#"]
    assert client.cb is not None


def test_check_turns_a_publish_into_an_envelope_named_file():
    ds, client = _source()
    client.pending.append((b"sensors/greenhouse/temp", b"21.5"))
    ds.check()
    assert ds.has_pending()
    file = ds.peek_file()
    topic, artifact_id, ts = decode_name(file.get_name())
    assert topic == "sensors/greenhouse/temp"
    assert ts is None                      # no timestamp_fn -> no fake clock shipped
    assert bytes(file.get_content()) == b"21.5"
    assert file.chunk_size == 200


def test_artifact_ids_make_repeated_payloads_distinct_files():
    # The base queue dedupes by name; the rolling id must keep two identical readings
    # from collapsing into one file.
    ds, client = _source()
    client.pending.append((b"sensors/t", b"7"))
    client.pending.append((b"sensors/t", b"7"))
    ds.check()
    ds.check()
    names = [f.get_name() for f in ds.file_queue]
    assert len(names) == 2 and names[0] != names[1]
    ids = [decode_name(n)[1] for n in names]
    assert ids[1] == ids[0] + 1


def test_timestamp_fn_rides_the_envelope():
    ds, client = _source(timestamp_fn=lambda: 123456)
    client.pending.append((b"sensors/t", b"x"))
    ds.check()
    assert decode_name(ds.peek_file().get_name())[2] == 123456


def test_check_with_nothing_pending_queues_nothing():
    ds, client = _source()
    ds.check()
    assert not ds.has_pending()


def test_close_disconnects_an_owned_client_only():
    # An injected client belongs to the caller (the sink's precedent): close() must not
    # tear down a connection it doesn't own.
    ds, client = _source()
    ds.close()
    assert not client.disconnected


def test_loop_guard_drops_what_the_paired_sink_just_published():
    # Bridge deployments run a datasource and a sink against the SAME broker: the sink's
    # republish echoes back into the subscription and must not ship over the link again.
    from AlLoRa.DataSources.Loop_guard import Loop_guard
    guard = Loop_guard()
    guard.note("sensors/t", b"21.5")
    ds, client = _source(loop_guard=guard)
    client.pending.append((b"sensors/t", b"21.5"))    # the echo
    client.pending.append((b"sensors/t", b"22.0"))    # a genuinely new reading
    ds.check()
    ds.check()
    names = [f.get_name() for f in ds.file_queue]
    assert len(names) == 1
    assert bytes(ds.peek_file().get_content()) == b"22.0"


def test_loop_guard_forgets_oldest_beyond_capacity():
    from AlLoRa.DataSources.Loop_guard import Loop_guard
    guard = Loop_guard(cap=2)
    guard.note("t", b"1")
    guard.note("t", b"2")
    guard.note("t", b"3")
    assert not guard.seen("t", b"1")     # evicted
    assert guard.seen("t", b"2")
    assert guard.seen("t", b"3")

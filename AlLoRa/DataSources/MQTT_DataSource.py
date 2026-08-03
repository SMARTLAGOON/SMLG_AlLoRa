from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.DataSources.mqtt_naming import encode_name
from AlLoRa.File import AlLoRa_File


class MQTT_DataSource(DataSource):
    """Subscribe to an MQTT broker and queue each matching PUBLISH as an AlLoRa_File.

    The ingest twin of MQTT_DataSink: the sink republishes a received file to a broker,
    this datasource turns a broker message into a file for whoever holds the source role
    to serve. The original topic (+ a rolling artifact id, + a timestamp when the caller
    can vouch for one) rides the file name as the shared envelope, so the paired sink on
    the far side republishes on the very same topic with the payload byte-for-byte.

    Runtime-agnostic like the sink: paho on a host, umqtt frozen on a board, both lazily
    imported; tests inject an umqtt-shaped duck-typed client (set_callback / subscribe /
    check_msg). check() is the non-blocking pump. The node loop shares it with the
    radio, so it must never block: umqtt's check_msg is non-blocking by contract, and
    paho delivers on its own network thread, leaving check() nothing to do.
    """

    def __init__(self, file_chunk_size, host="localhost", port=1883,
                 topics=("#",), client=None, client_id="allora-datasource",
                 keepalive=60, file_queue_size=25, timestamp_fn=None, loop_guard=None):
        super().__init__(file_chunk_size, file_queue_size=file_queue_size)
        self.host = host
        self.port = port
        self.topics = tuple(topics)
        self.client_id = client_id
        self.keepalive = keepalive
        # No timestamp_fn -> no timestamp in the envelope: an unsynced tick counter
        # shipped as a wall clock would be worse than nothing.
        self._timestamp_fn = timestamp_fn
        self._loop_guard = loop_guard
        # The artifact id is a RAM counter: it exists to keep same-topic same-payload
        # messages distinct in the name-deduped queue, not to survive reboots.
        self._artifact_id = 0
        self._client = client             # injected -> we don't own it; else built in prepare()
        self._owns_client = client is None
        self._flavor = "injected" if client is not None else None

    # -- lifecycle -----------------------------------------------------------------------------

    def prepare(self):
        if self._client is None:
            self._client = self._build_client()
        if hasattr(self._client, "set_callback"):     # umqtt-shaped (incl. injected fakes)
            self._client.set_callback(self._on_message)
        for topic in self.topics:
            if self._flavor == "umqtt" and isinstance(topic, str):
                topic = topic.encode()                # umqtt wants bytes topics
            self._client.subscribe(topic)

    def check(self):
        # umqtt (and the umqtt-shaped fakes) deliver at most one queued message per
        # non-blocking check_msg; paho has no such verb: its loop thread already
        # invoked _on_message, so this degrades to a no-op there.
        check_msg = getattr(self._client, "check_msg", None)
        if check_msg is not None:
            check_msg()

    def close(self):
        if self._owns_client and self._client is not None:
            try:
                if self._flavor == "paho":
                    self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            self._client = None

    # -- internals -----------------------------------------------------------------------------

    def _on_message(self, topic, payload):
        if isinstance(topic, (bytes, bytearray)):
            topic = bytes(topic).decode("utf-8")
        payload = bytes(payload) if payload else b""
        if self._loop_guard is not None and self._loop_guard.seen(topic, payload):
            # Our paired sink just published this very message to this broker; shipping
            # it back over the link would ping-pong forever.
            return
        ts = self._timestamp_fn() if self._timestamp_fn is not None else None
        self._artifact_id = (self._artifact_id + 1) & 0xFFFFFFFF
        name = encode_name(topic, self._artifact_id, ts)
        self.add_to_queue(AlLoRa_File(name=name, content=bytearray(payload),
                                      chunk_size=self.file_chunk_size))

    def _build_client(self):
        # Host -> paho; on-device -> umqtt. Lazy imports keep both optional.
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            mqtt = None
        if mqtt is not None:
            client = mqtt.Client(client_id=self.client_id)
            client.on_message = self._paho_on_message
            client.connect(self.host, self.port, self.keepalive)
            client.loop_start()
            self._flavor = "paho"
            return client

        try:
            from umqtt.robust import MQTTClient
        except ImportError:
            from umqtt.simple import MQTTClient
        client = MQTTClient(self.client_id, self.host, self.port, keepalive=self.keepalive)
        client.connect()
        self._flavor = "umqtt"
        return client

    def _paho_on_message(self, client, userdata, message):
        self._on_message(message.topic, message.payload)

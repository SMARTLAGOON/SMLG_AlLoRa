from AlLoRa.DataSinks.DataSink import DataSink


class MQTT_DataSink(DataSink):
    """Publish each received file to an MQTT broker instead of saving it to disk.

    The Collector reassembles a file, then this sink pushes its bytes to
    `<topic_prefix>/<source>/<filename>` (override with `topic_for`). It is the in-library twin
    of the external MQTT->InfluxDB->Grafana pipeline student deployments already run: the file
    bytes are the payload verbatim, so a JSON metric file lands ready for a telegraf mqtt_consumer,
    and an image file lands as raw bytes on its own topic.

    Runtime-agnostic: on a host Collector (CPython) it drives paho-mqtt; frozen on an ESP32-class
    board it drives umqtt. Both are imported lazily so this module never drags a broker client into
    a deployment that doesn't publish, and so the unused runtime's library need not be installed.
    Tests (and any custom transport) inject a duck-typed `client` with a `publish(topic, payload)`.
    """

    def __init__(self, host="localhost", port=1883, topic_prefix="allora",
                 client=None, topic_for=None, client_id="allora-collector",
                 qos=0, retain=False, keepalive=60, cleanup=True):
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix
        self.client_id = client_id
        self.qos = qos
        self.retain = retain
        self.keepalive = keepalive
        self.cleanup = cleanup            # discard the reassembly temp after publishing
        self._topic_for = topic_for       # optional callable(source, filename) -> topic str
        self._client = client             # injected -> we don't own it; else built in prepare()
        self._owns_client = client is None
        self._flavor = "injected" if client is not None else None

    # -- lifecycle -----------------------------------------------------------------------------

    def prepare(self):
        if self._client is None:
            self._client = self._build_client()

    def consume(self, file, reception=None):
        if self._client is None:
            self.prepare()
        source = reception.source if reception is not None else None
        topic = self._topic(source, file.get_name())
        payload = bytes(file.get_content())
        self._publish(topic, payload)
        if self.cleanup:
            try:
                file.discard()
            except Exception:
                pass

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

    def _topic(self, source, filename):
        if self._topic_for is not None:
            return self._topic_for(source, filename)
        parts = [self.topic_prefix, source, filename]
        return "/".join(p for p in parts if p)

    def _publish(self, topic, payload):
        if self._flavor == "paho":
            self._client.publish(topic, payload, qos=self.qos, retain=self.retain)
        elif self._flavor == "umqtt":
            t = topic.encode() if isinstance(topic, str) else topic
            p = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload)
            self._client.publish(t, p, self.retain, self.qos)
        else:  # injected / custom client — duck-typed 2-arg publish
            self._client.publish(topic, payload)

    def _build_client(self):
        # Host Collector -> paho; on-device -> umqtt. Lazy imports keep both optional.
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            mqtt = None
        if mqtt is not None:
            client = mqtt.Client(client_id=self.client_id)
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

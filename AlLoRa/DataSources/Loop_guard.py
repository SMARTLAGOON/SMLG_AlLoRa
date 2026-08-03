class Loop_guard:
    """A tiny LRU set of recently self-published messages, shared by a paired
    MQTT_DataSource + MQTT_DataSink working the SAME broker (the bridge deployment).

    The sink note()s every message it republishes; the datasource's subscription hears
    that republish echoed back and seen() tells it to drop it instead of shipping it
    over the link again. Without the guard the pair ping-pongs one message forever.
    A message is remembered by hash, bounded by `cap` (RAM-fixed on-device); seen()
    does not consume, because a retained or re-delivered echo can arrive more than once.
    """

    def __init__(self, cap=64):
        self.cap = int(cap)
        self._order = []
        self._known = set()

    @staticmethod
    def _key(topic, payload):
        if isinstance(topic, str):
            topic = topic.encode("utf-8")
        payload = bytes(payload) if payload else b""
        return hash((bytes(topic), payload))

    def note(self, topic, payload):
        key = self._key(topic, payload)
        if key in self._known:
            return
        self._known.add(key)
        self._order.append(key)
        if len(self._order) > self.cap:
            self._known.discard(self._order.pop(0))

    def seen(self, topic, payload):
        return self._key(topic, payload) in self._known

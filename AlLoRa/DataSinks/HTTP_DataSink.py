from AlLoRa.DataSinks.DataSink import DataSink


class HTTP_DataSink(DataSink):
    """POST each received file to a web service instead of saving it to disk.

    The third boundary beside disk and MQTT, and the one that closes the last leg of a
    deployment: Edge reaches Hub over the air, Hub reaches a site over the network, and a person
    downloads the file from the site. It exists because a Hub is behind NAT and asleep half the
    time, so nothing can reach *in* to fetch what it holds. The Hub pushes the instant a file is
    whole, which is the same shape the protocol already has on the air (the collector role drives
    and pulls, and here it drives and pushes).

    The file bytes are the request body verbatim, with the Reception snapshot in `X-AlLoRa-*`
    headers. Not multipart and not base64-in-JSON: MicroPython has no multipart helper, and
    base64 would inflate a payload that a radio spent minutes delivering by another third while
    forcing a second copy of it into a heap that has no room for one.

    Runtime-agnostic in the same way MQTT_DataSink is: on a host it drives `requests`, frozen on
    an ESP32-class board it drives `urequests`, and both are imported lazily so this module never
    drags an HTTP client into a deployment that does not post. The two libraries already agree on
    the only call this makes, so unlike the MQTT pair there is no flavor to track: a client is
    anything with `post(url, data=..., headers=...)` returning an object with `.status_code`.
    Tests (and any custom transport) inject one.
    """

    def __init__(self, url=None, token=None, timeout=10, cleanup=True,
                 client=None, headers=None):
        # Fail closed at construction. MQTT_DataSink may default its host because a Hub
        # co-located with its broker is the ordinary deployment, so `localhost` is a guess that
        # is usually right. There is no equivalent guess for a site: a sink built without one
        # would accept every file and deliver none.
        if not url:
            raise ValueError(
                "HTTP_DataSink needs the url it posts to; there is no sensible default for "
                "where a deployment's data goes")
        self.url = url
        self.token = token
        self.timeout = timeout
        self.cleanup = cleanup            # discard the reassembly temp after a confirmed post
        self.extra_headers = headers or {}
        self._client = client             # injected -> we don't own it; else found in prepare()

    # -- lifecycle -----------------------------------------------------------------------------

    def prepare(self):
        if self._client is None:
            self._client = self._find_client()

    def consume(self, file, reception=None):
        if self._client is None:
            self.prepare()
        payload = bytes(file.get_content())
        self._post(payload, self._headers(file.get_name(), reception))
        # Cleanup means "the service has taken ownership, so the temp is no longer the only
        # copy". That is only true after a confirmed post, which is why _post raises instead of
        # returning a flag: the failure path never reaches this line, and the node's caller owns
        # it from there (discard, rewind the endpoint, pull the file again next round).
        if self.cleanup:
            try:
                file.discard()
            except Exception:
                pass

    def close(self):
        # Genuinely nothing to release: both clients make a connection per call and each
        # response is closed as it is read. Deliberately not dropping `_client` either, so a
        # close() and a later consume() do not quietly swap an injected client for the real
        # library. Defined so the lifecycle reads like the other sinks'.
        pass

    # -- internals -----------------------------------------------------------------------------

    def _headers(self, filename, reception):
        headers = {"Content-Type": "application/octet-stream"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if filename is not None:
            headers["X-AlLoRa-Filename"] = filename
        if reception is not None:
            # Only what the transfer actually knew. An absent header says "this deployment does
            # not have that fact" (device_id is None in open mode, RSSI can be missing on a
            # loopback), which a receiver can act on; a header carrying "None" is a string that
            # looks like data and lands in a database as one.
            for name, value in (
                    ("X-AlLoRa-Source", reception.source),
                    ("X-AlLoRa-Session-Id", reception.session_id),
                    ("X-AlLoRa-Device-Id", reception.device_id),
                    ("X-AlLoRa-Rssi", reception.rssi),
                    ("X-AlLoRa-Snr", reception.snr),
                    ("X-AlLoRa-Total-Chunks", reception.total_chunks),
                    ("X-AlLoRa-Timestamp-Ms", reception.timestamp_ms)):
                if value is not None:
                    headers[name] = str(value)
        headers.update(self.extra_headers)
        return headers

    def _post(self, payload, headers):
        # `timeout` is passed only when it is set. Recent MicroPython clients take it and older
        # urequests builds do not, so a board whose client rejects the keyword is configured
        # with `"timeout": null` rather than needing a different sink.
        kwargs = {"data": payload, "headers": headers}
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        response = self._client.post(self.url, **kwargs)
        status = getattr(response, "status_code", None)
        try:
            if status is None or not (200 <= status < 300):
                # Raise rather than log. Node's consume call site treats an exception as "this
                # file was not delivered": it discards the temp, rewinds the endpoint and pulls
                # the file again next round. Swallowing the failure here would turn a site
                # outage into silent data loss, which is the one thing a sink must never do.
                raise OSError(
                    "posting {} to {} returned {}".format(
                        headers.get("X-AlLoRa-Filename", "a file"), self.url,
                        status if status is not None else "no status"))
        finally:
            # urequests holds the socket until the response is closed, and a Hub that leaked one
            # per file would run out within a day of ordinary collection.
            closer = getattr(response, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:
                    pass

    @staticmethod
    def _find_client():
        # Host Hub -> requests; on-device -> urequests. Lazy so neither is a dependency of a
        # deployment that does not post.
        try:
            import requests
            return requests
        except ImportError:
            import urequests
            return urequests

"""A status subscriber that posts the live transfer to a web service.

The third thing that can watch a node's status dict, beside a screen and the Logger, and the
only one a person can read from somewhere else. What it answers is the question a Hub cannot
answer today: a file has been arriving for eleven minutes, and the only place that knows the
name it is arriving under, how many chunks are left and what the link is doing is a terminal
on the gateway.

It lives here rather than in a deployment because it decides nothing. Give it a URL and how
often to talk, and it moves values that already exist; where the data goes and what it means
there is the site's business, and which node is worth watching is the operator's. A
DataSink is the other half of this and deliberately not the same thing: a sink carries the
artifact, once, and must never lose it; this carries an impression of the moment, often, and
losing one costs nothing because a fresher one is already on its way.

Two rules make it safe to attach to a running Hub, and both matter more than anything it does:

**It never posts from the radio loop.** `update()` is called from inside the drive loop, once
per chunk, and the loop's next act is to ask for another chunk. A synchronous request there
would put the site's latency between two radio exchanges, and a site that has stopped
answering would stall a transfer for the length of an HTTP timeout, over and over. So
`update()` only takes a snapshot and returns, and a worker thread does the talking.

**It drops what it could not send.** Only the newest snapshot is kept. A slow site does not
build a queue of progress reports that were true a minute ago, because nobody wants those: the
value of this data is entirely in it being current, which is the opposite of the sink's
contract next door. A missed tick is not an error and is never retried.

**It keeps saying so when nothing is happening.** A node notifies only while it is doing
something, and a Hub between visits sleeps for its asking_frequency, a minute by default. A
receiver that expires a snapshot in thirty seconds would watch the gateway appear and vanish
every minute, so the worker repeats the last snapshot on the interval. That separates the two
questions a page actually has, "is the gateway there" and "is a file moving", instead of
answering both with one silence.

Failures are swallowed on purpose, and this is the one place in the library where that is the
right answer. A node's job is to move files. If the status service is down, or wrong about its
token, or gone, the transfer must not notice.
"""
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.json_utils import json
from AlLoRa.utils.time_utils import current_time_ms, sleep_ms, ticks_diff

# How often the worker wakes to look for something to send. Short enough that a transition
# (a new file, a finished one) reaches the site promptly without its own signalling
# machinery, long enough to be free: MicroPython's _thread has no Event to wait on, and a
# poll at this rate costs less than the radio's own idle sleep.
_TICK_MS = 200


class HTTP_Status_Subscriber:
    """Post the node's live status to `url` at most every `interval_ms`.

    A subscriber is anything with `update(status)`, so this attaches with
    `node.register_subscriber(sub)` and needs nothing else from the node. Call `start()` to
    bring the worker up; until then it is inert and costs one dict per notify.

    `interval_ms` is a ceiling on ordinary progress, not on everything: a change of file, of
    node, or of state is sent as soon as the worker sees it, because those are the moments a
    person watching the page is actually waiting for.
    """

    def __init__(self, url=None, token=None, interval_ms=2000, timeout=5,
                 client=None, headers=None, debug=False):
        # Fail closed at construction, for the same reason HTTP_DataSink does: there is no
        # sensible default for where a deployment's telemetry goes, and a subscriber built
        # without one would tick forever and report nothing.
        if not url:
            raise ValueError(
                "HTTP_Status_Subscriber needs the url it posts to; there is no sensible "
                "default for where a deployment's status goes")
        self.url = url
        self.token = token
        self.interval_ms = interval_ms
        self.timeout = timeout
        self.extra_headers = headers or {}
        self.debug = debug
        self._client = client          # injected -> we don't own it; else found on first post
        self._pending = None           # the newest snapshot, or None if the worker took it
        self._last_sent = None         # what went last, re-sent as the heartbeat
        self._last_key = None          # what the last *sent* snapshot was about, for transitions
        self._last_sent_ms = None
        self._running = False

    # -- lifecycle -----------------------------------------------------------------------

    def start(self):
        """Bring the worker up. Idempotent, so a restarted node cannot end up with two."""
        if self._running:
            return
        # Imported here rather than at the top, the way DataSource.start does it: the module
        # has to load on a build compiled without _thread, where everything above still works
        # and only the worker is unavailable.
        import _thread
        self._running = True
        _thread.start_new_thread(self._pump, ())

    def stop(self):
        self._running = False

    # -- the subscriber verb, called from the radio loop ---------------------------------

    def update(self, status):
        """Take a snapshot and return. Nothing here may block, allocate much, or raise.

        The snapshot is a fresh flat dict of scalars rather than a reference into `status`.
        The node updates that dict in place and hands out the live `file_reception_info` of
        an endpoint it goes on mutating, so a reference kept here would be read by the worker
        halfway through the next chunk and report a mix of two moments.
        """
        try:
            self._pending = self._snapshot(status)
        except Exception:
            # A subscriber that throws would come out of notify() inside the drive loop.
            # Reporting is never worth a transfer.
            pass

    # -- what gets sent --------------------------------------------------------------------

    @staticmethod
    def _scalar(value):
        # The node writes "-" into every live field at startup and leaves it there until the
        # first packet. Absent says "not measured yet", which a page can render honestly; "-"
        # is a string that has to be special-cased by every reader downstream.
        return None if value == "-" else value

    def _snapshot(self, status):
        label = self._scalar(status.get("SMAC"))
        endpoint = (status.get("Digital_Endpoints") or {}).get(label) or {}
        chunks_left = self._scalar(status.get("Chunk"))
        return {
            "hub": status.get("MAC"),
            "node": label,                 # the peer being driven right now, None between visits
            "state": self._scalar(status.get("Status")),
            # The receiving side knows the name from the endpoint it is reassembling into; the
            # serving side writes it flat. Taking both makes this the same class on an Edge.
            "file": endpoint.get("current_receiving_file_name") or self._scalar(
                status.get("File")),
            "chunks_total": endpoint.get("total_chunks"),
            # A countdown while a file is in flight, the string DONE at the end of one. Passed
            # through as the node wrote it: inventing a number for DONE would make a finished
            # transfer indistinguishable from one with nothing left to ask for.
            "chunks_left": chunks_left,
            "rssi": self._scalar(status.get("RSSI")),
            "snr": self._scalar(status.get("SNR")),
            "sf": status.get("SF"),
            "bw": status.get("BW"),
            "cr": status.get("CR"),
            "tx_power": status.get("TX_P"),
            "freq": status.get("Freq"),
            "corrupted": status.get("CorruptedPackets"),
            "retransmissions": status.get("Retransmission"),
            "at": current_time_ms(),
        }

    @staticmethod
    def _key(snapshot):
        # What makes a snapshot worth sending immediately rather than at the next interval.
        # Not the chunk count: that changes every tick, which would make every tick urgent and
        # the interval meaningless.
        return (snapshot.get("node"), snapshot.get("file"), snapshot.get("state"),
                snapshot.get("chunks_left") == "DONE")

    # -- the worker ------------------------------------------------------------------------

    def _pump(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:
                # Belt and braces around the whole body: this thread dying would take live
                # progress with it silently, and the node would never know.
                if self.debug:
                    print("[status] worker error: {}".format(e))
            sleep_ms(_TICK_MS)

    def _tick(self):
        snapshot = self._pending
        if snapshot is None:
            # Nothing new, so say the last thing again if the interval has passed.
            #
            # This is the heartbeat, and it is not busywork. A node notifies only while it is
            # doing something: a Hub between visits sleeps for its asking_frequency, which is
            # a minute by default, and a receiver reading a service that expires in thirty
            # seconds would watch the gateway appear and vanish every minute. Repeating the
            # last snapshot separates the two questions a page actually has, "is the gateway
            # there" and "is a file moving", and answers the first one honestly while the
            # second is no.
            #
            # Nothing is invented in the repeat: the same values go out unchanged, including
            # the node's own `at`. What makes it current is the receiver's own clock, which
            # is the only one either end should trust for that.
            if self._last_sent is not None and self._elapsed():
                self._send(self._last_sent)
                self._last_sent_ms = current_time_ms()
            return
        key = self._key(snapshot)
        if not (key != self._last_key or self._elapsed()):
            return
        # Claim it before sending, not after. A snapshot written by the radio loop between
        # these two statements is lost, which is exactly the intended behaviour: the next one
        # is already truer than the one in flight.
        self._pending = None
        if self._send(snapshot):
            self._last_key = key
            self._last_sent = snapshot
            self._last_sent_ms = current_time_ms()

    def _elapsed(self):
        if self._last_sent_ms is None:
            return True
        return ticks_diff(current_time_ms(), self._last_sent_ms) >= self.interval_ms

    def _send(self, snapshot):
        try:
            if self._client is None:
                self._client = self._find_client()
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            headers.update(self.extra_headers)
            kwargs = {"data": json.dumps(snapshot), "headers": headers}
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            response = self._client.post(self.url, **kwargs)
            try:
                status_code = getattr(response, "status_code", None)
                ok = status_code is not None and 200 <= status_code < 300
                if not ok and self.debug:
                    print("[status] {} refused the snapshot: {}".format(self.url, status_code))
                return ok
            finally:
                # urequests holds the socket until the response is closed, and this posts far
                # more often than the sink does.
                closer = getattr(response, "close", None)
                if closer is not None:
                    try:
                        closer()
                    except Exception:
                        pass
        except Exception as e:
            if self.debug:
                print("[status] could not reach {}: {}".format(self.url, e))
            return False

    @staticmethod
    def _find_client():
        # Host Hub -> requests; on-device -> urequests. Lazy, so neither is a dependency of a
        # deployment that does not report.
        try:
            import requests
            return requests
        except ImportError:
            import urequests
            return urequests

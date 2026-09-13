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

**It costs the site less every time the site says no.** An attempt is paid for whether or not
it is delivered, and a run of refusals doubles the wait up to five minutes. This is not
politeness. A gateway whose refused posts were free once spent a month of a free database's
allowance in three days, against a site that answered every one of them with an error, while
the person watching saw nothing at all. A refusal is the one answer that must never be cheaper
than a success.

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

# The longest a run of refusals may push the next attempt out. Five minutes is long enough
# that a site down all night costs a few hundred requests instead of a few hundred thousand,
# and short enough that a gateway does not look dead for hours after the site comes back.
_MAX_BACKOFF_MS = 300_000


class HTTP_Status_Subscriber:
    """Post the node's live status to `url` at most every `interval_ms`.

    A subscriber is anything with `update(status)`, so this attaches with
    `node.register_subscriber(sub)` and needs nothing else from the node. Call `start()` to
    bring the worker up; until then it is inert and costs one dict per notify.

    `interval_ms` is a ceiling on ordinary progress, not on everything: a change of file, of
    node, or of state is sent as soon as the worker sees it, because those are the moments a
    person watching the page is actually waiting for. The one thing that does not jump the
    interval is a transition arriving while the site is refusing: a new file does not change
    the mind of a service that is saying no.

    Thirty seconds by default. Two, which this used to be, is 43,200 posts a day from a single
    Hub, and a receiver reading a countdown that changes every chunk does not need it that
    often. A deployment that wants it faster passes a smaller number knowingly.
    """

    def __init__(self, url=None, token=None, interval_ms=30000, timeout=5,
                 client=None, headers=None, debug=False, now_ms=None):
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
        # The clock, injectable for the same reason the client is: every rule here is about
        # how much time has passed, and a test that has to wait five real minutes to check a
        # five minute backoff is a test nobody runs.
        self._now = now_ms or current_time_ms
        self._pending = None           # the newest snapshot, or None if the worker took it
        self._last_sent = None         # what went last, re-sent as the heartbeat
        self._last_key = None          # what the last *sent* snapshot was about, for transitions
        self._last_sent_ms = None
        self._failures = 0             # refusals in a row; zero means the site is answering
        self._retry_after_ms = None    # what the site asked for, when it asked for anything
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
            "at": self._now(),
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
                self._attempt(self._last_sent)
            return
        key = self._key(snapshot)
        # A transition jumps the interval, because a new file or a finished one is the moment
        # a person watching the page is waiting for. It does not jump a refusal: while the
        # site is saying no, nothing here is urgent enough to ask again early.
        urgent = key != self._last_key and not self._backing_off()
        if not (urgent or self._elapsed()):
            return
        # Claim it before sending, not after. A snapshot written by the radio loop between
        # these two statements is lost, which is exactly the intended behaviour: the next one
        # is already truer than the one in flight.
        self._pending = None
        if self._attempt(snapshot):
            # Only a delivered snapshot becomes the thing to repeat and the thing later
            # snapshots are compared against. A refusal is an attempt, not news the site has.
            self._last_key = key
            self._last_sent = snapshot

    def _attempt(self, snapshot):
        """Post it, and pay for the attempt whatever the answer comes back as.

        The clock moves here rather than on success, which is the whole of the fix: leaving it
        alone after a refusal made every later snapshot the first news about that file again,
        so the transition shortcut fired on every worker tick for as long as the site refused.
        """
        self._last_sent_ms = self._now()
        if self._send(snapshot):
            self._failures = 0
            self._retry_after_ms = None
            return True
        # Counted only while counting still changes the answer. Once the wait is at the
        # ceiling the number has no further use, and a counter climbing all week is just a
        # bigger integer for a board to hold.
        if self._wait_ms() < _MAX_BACKOFF_MS:
            self._failures += 1
        return False

    def _elapsed(self):
        if self._last_sent_ms is None:
            return True
        return ticks_diff(self._now(), self._last_sent_ms) >= self._wait_ms()

    def _backing_off(self):
        """Whether the site is currently refusing us, by either of the two ways it can.

        Its own question rather than a reading of the failure counter: a site that names a
        wait long enough to reach the ceiling leaves that counter at zero, and a transition
        must not read that as a healthy site and jump the queue.
        """
        return bool(self._failures or self._retry_after_ms)

    def _wait_ms(self):
        """How long this owes the site before the next attempt.

        The interval while the site is answering, and twice as long again for every refusal in
        a row. A site that is down is usually down for a while: asking it at the ordinary
        interval is still tens of thousands of refused requests a day, which is what emptied a
        month of database allowance in three days.
        """
        # A site that named a number outranks any number of ours: it is the one enforcing the
        # limit, and it knows when it will lift.
        if self._retry_after_ms:
            return self._retry_after_ms
        if not self._failures:
            return self.interval_ms
        # Doubling zero is zero, so a subscriber told to post on every tick backs off from the
        # tick instead. The rule being kept is that a refusal costs something.
        wait = self.interval_ms or _TICK_MS
        failures = self._failures
        # Stops at the ceiling rather than shifting by the failure count: a gateway refused all
        # week would otherwise be doubling an integer past anything an ESP32 wants to hold.
        while failures and wait < _MAX_BACKOFF_MS:
            wait += wait
            failures -= 1
        return _MAX_BACKOFF_MS if wait > _MAX_BACKOFF_MS else wait

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
                if not ok:
                    self._retry_after_ms = self._retry_after(response, status_code)
                    if self.debug:
                        print("[status] {} refused the snapshot: {}".format(
                            self.url, status_code))
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
            # A site we could not reach has asked for nothing, so anything it asked for
            # earlier is stale and the backoff takes over again.
            self._retry_after_ms = None
            if self.debug:
                print("[status] could not reach {}: {}".format(self.url, e))
            return False

    @staticmethod
    def _retry_after(response, status_code):
        """The wait the site asked for, in milliseconds, or None if it asked for nothing.

        Only 429 and 503 are honoured. Those two mean "not now"; a 500 with a Retry-After is a
        site that is broken rather than busy, and its guess about its own repair is worth less
        than backing off.
        """
        if status_code != 429 and status_code != 503:
            return None
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        value = None
        for name in ("Retry-After", "retry-after"):
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value:
                break
        if not value:
            return None
        try:
            seconds = int(str(value).strip())
        except Exception:
            # The header is allowed to carry an HTTP date instead of a count of seconds.
            # Reading one needs a date library the board does not carry, so the backoff
            # answers for it rather than a crash or a zero.
            return None
        return seconds * 1000 if seconds > 0 else None

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

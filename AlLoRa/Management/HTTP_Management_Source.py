"""A management source that asks a web service what this Hub should be running.

The transport half of the management plane, and the first implementation of the boundary next
door. The slot, the drain and applying a change already existed and were proven on real radio;
this is the class that goes and asks, and it was deliberately left until the site had a route
to answer it.

**One request, both directions.** The Hub's report is the body and the wish is the answer,
because the two are one exchange: this is what I am running, and the reply is what I should be.
Splitting it would put two round trips between two radio exchanges and let a site answer a wish
against a report it had not yet read.

**Nothing is delivered, so nothing is retried.** This is not the sink's contract, where an
artifact must arrive exactly once, nor the subscriber's, where the newest impression replaces
the last. It carries state: a refused exchange costs one interval of staleness, after which the
same wish is still the wish. There is no queue, no acknowledgement and no bookkeeping, which is
what makes the loop self-healing with no mechanism.

**It never asks from the radio loop on a host.** `check()` is called from the Hub's drain,
between two visits, and the loop's next act is to listen to an endpoint. A site that has
stopped answering would otherwise put its whole timeout there, over and over. So on a host a
worker thread does the talking and `check()` returns at once. On a microcontroller acting alone
as a Hub there is no thread to spare, and the loop does the asking itself; that is the one slot
with two fillers, and it is why both live in the library rather than in a deployment.

**A refusal costs more than a success.** The same rule the status subscriber learned expensively:
a gateway whose refused posts were free once spent a month of a free database's allowance in
three days, against a site that answered every one of them with an error.

Failures are swallowed, as they are next door and for the same reason. A node's job is to move
files. If the site is down, or wrong about its token, or gone, the transfer must not notice.
"""
from AlLoRa.Management.Management_Source import Management_Source
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.json_utils import json
from AlLoRa.utils.time_utils import current_time_ms, sleep_ms, ticks_diff

# How often the worker wakes to see whether an exchange is due. The same tick the status
# subscriber uses, and for the same reason: MicroPython's _thread has no Event to wait on, and
# a poll at this rate costs less than the radio's own idle sleep.
_TICK_MS = 200

# The longest a run of refusals may push the next ask out. Five minutes is long enough that a
# site down all night costs a few hundred requests instead of a few hundred thousand, and short
# enough that a change made while it was down lands soon after it comes back.
_MAX_BACKOFF_MS = 300_000


class HTTP_Management_Source(Management_Source):
    """Ask `url` what this Hub should be running, at most every `interval_s`.

    Register it with `hub.set_management_source(...)`, which calls `prepare()` for you, so that
    a connect that must fail fails loudly at setup rather than part way through a drive loop.

    `interval_s` is the opening interval only. The site sends `poll_interval_s` with every
    answer and that becomes the interval from then on, because how often a Hub asks is desired
    state like everything else here: a person changes it on the page, for one Hub, without
    touching the box. The bounds on what is sensible are the site's own policy and are
    deliberately not repeated in the library.

    `threaded` decides who does the asking. None, the default, means decide by whether this
    build has `_thread`: a host gets a worker, a board without one is pumped by the visit loop.
    Pass False on a host to put the asking back on the loop knowingly.
    """

    def __init__(self, url=None, token=None, interval_s=30, timeout=5,
                 client=None, headers=None, debug=False, now_ms=None, threaded=None):
        Management_Source.__init__(self)
        # Fail closed at construction, as HTTP_DataSink and the status subscriber do: there is
        # no sensible default for which website owns a deployment, and a source built without
        # one would poll forever and learn nothing.
        if not url:
            raise ValueError(
                "HTTP_Management_Source needs the url it asks; there is no sensible default "
                "for which website holds a deployment's desired state")
        self.url = url
        self.token = token
        self.interval_ms = int(interval_s * 1000)
        self.timeout = timeout
        self.extra_headers = headers or {}
        self.debug = debug
        self._client = client          # injected -> we don't own it; else found on first ask
        # The clock, injectable for the same reason the client is: every rule here is about how
        # much time has passed, and a test that waits five real minutes is a test nobody runs.
        self._now = now_ms or current_time_ms
        self._threaded = threaded
        self._running = False
        self._last_ms = None           # when the last attempt was paid for
        self._failures = 0             # refusals in a row; zero means the site is answering
        self._retry_after_ms = None    # what the site asked for, when it asked for anything

    # -- lifecycle -------------------------------------------------------------------------

    def prepare(self):
        """Decide who does the asking, and bring up a worker if it is not the loop.

        Idempotent, so a node registered twice cannot end up with two workers talking to the
        same site about the same Hub.
        """
        if self._running:
            return
        if self._threaded is None:
            self._threaded = self._has_thread()
        if not self._threaded:
            return
        # Imported here rather than at the top, the way the subscriber's worker is: the module
        # has to load on a build compiled without _thread, where everything above still works
        # and only the worker is unavailable.
        import _thread
        self._running = True
        _thread.start_new_thread(self._pump, ())

    def close(self):
        """Stop the worker. Idempotent, and safe on a source the loop was pumping."""
        self._running = False

    def is_running(self):
        """Whether a worker is doing the asking. False when the visit loop is."""
        return self._running

    # -- the pump ---------------------------------------------------------------------------

    def check(self):
        """Called by the visit loop between visits.

        A no-op while a worker holds the asking, which is the whole point of the worker: this
        returns in the time it takes to read a flag, however long the site is taking.
        """
        if self._running:
            return
        self._tick()

    def _pump(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:
                # Belt and braces around the whole body: this thread dying would leave the Hub
                # silently unmanageable, still running and never asking again.
                if self.debug:
                    print("[management] worker error: {}".format(e))
            sleep_ms(_TICK_MS)

    def _tick(self):
        # Nothing to say yet. The site's route answers a report, so an exchange before the first
        # visit is a request that can only come back a 400, and it would be paid for like any
        # other.
        if self.last_report() is None:
            return
        if not self._elapsed():
            return
        self._attempt()

    # -- the exchange -------------------------------------------------------------------------

    def _attempt(self):
        """Ask once, and pay for the attempt whatever the answer comes back as.

        The clock moves here rather than on success, which is what makes a refusal cost
        something: leaving it alone after a refusal would make the next tick due immediately and
        turn a site that is down into a spin.
        """
        self._last_ms = self._now()
        intent = self._ask()
        if intent is None:
            # Counted only while counting still changes the answer. Once the wait is at the
            # ceiling the number has no further use, and a counter climbing all week is just a
            # bigger integer for a board to hold.
            if self._wait_ms() < _MAX_BACKOFF_MS:
                self._failures += 1
            return
        self._failures = 0
        self._retry_after_ms = None
        self._receive(intent)

    def _receive(self, intent):
        """Take the wish, and read the one key in it that is this boundary's own business.

        `poll_interval_s` is read here rather than by the Hub because the Hub applies a roster
        and does not care how the wish arrived. Only a positive number is taken: the bounds on
        what is sensible belong to the site, but a zero or a string would turn the management
        channel into a spin, and that is a fault in the loop rather than a matter of policy.
        """
        interval = intent.get("poll_interval_s")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0:
            self.interval_ms = int(interval * 1000)
        self.submit_intent(intent)

    def _ask(self):
        """The wish the site holds, or None if it did not give us one."""
        try:
            if self._client is None:
                self._client = self._find_client()
            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            headers.update(self.extra_headers)
            kwargs = {"data": json.dumps(self.last_report()), "headers": headers}
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            response = self._client.post(self.url, **kwargs)
            try:
                status_code = getattr(response, "status_code", None)
                if status_code is None or not 200 <= status_code < 300:
                    self._retry_after_ms = self._retry_after(response, status_code)
                    if self.debug:
                        print("[management] {} refused the exchange: {}".format(
                            self.url, status_code))
                    return None
                return self._wish(response)
            finally:
                # urequests holds the socket until the response is closed, and this asks every
                # interval for the life of the deployment.
                closer = getattr(response, "close", None)
                if closer is not None:
                    try:
                        closer()
                    except Exception:
                        pass
        except Exception as e:
            # A site we could not reach has asked for nothing, so anything it asked for earlier
            # is stale and the backoff takes over again.
            self._retry_after_ms = None
            if self.debug:
                print("[management] could not reach {}: {}".format(self.url, e))
            return None

    @staticmethod
    def _wish(response):
        """The answer as a wish, or None if it is not one.

        Checked rather than trusted, because `Hub._apply_intent` reads this object directly. A
        proxy's login page, a truncated body and a site half way through a deploy all arrive as
        a 200 carrying something that is not a roster, and a list or a string read as one is how
        a gateway would be told to disable every node it holds.
        """
        try:
            reader = getattr(response, "json", None)
            wish = reader() if reader is not None else json.loads(response.text)
        except Exception:
            return None
        return wish if isinstance(wish, dict) else None

    # -- what it costs -------------------------------------------------------------------------

    def _elapsed(self):
        if self._last_ms is None:
            return True
        return ticks_diff(self._now(), self._last_ms) >= self._wait_ms()

    def _wait_ms(self):
        """How long this owes the site before the next ask.

        The interval while the site is answering, and twice as long again for every refusal in
        a row. A site that is down is usually down for a while, and asking it at the ordinary
        interval is still thousands of refused requests a day.
        """
        # A site that named a number outranks any number of ours: it is the one enforcing the
        # limit, and it knows when it will lift.
        if self._retry_after_ms:
            return self._retry_after_ms
        if not self._failures:
            return self.interval_ms
        # Doubling zero is zero, so a source told to ask on every tick backs off from the tick
        # instead. The rule being kept is that a refusal costs something.
        wait = self.interval_ms or _TICK_MS
        failures = self._failures
        # Stops at the ceiling rather than shifting by the failure count: a gateway refused all
        # week would otherwise be doubling an integer past anything an ESP32 wants to hold.
        while failures and wait < _MAX_BACKOFF_MS:
            wait += wait
            failures -= 1
        return _MAX_BACKOFF_MS if wait > _MAX_BACKOFF_MS else wait

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
            # The header is allowed to carry an HTTP date instead of a count of seconds. Reading
            # one needs a date library the board does not carry, so the backoff answers for it
            # rather than a crash or a zero.
            return None
        return seconds * 1000 if seconds > 0 else None

    # -- what this build has ---------------------------------------------------------------------

    @staticmethod
    def _has_thread():
        try:
            import _thread                                        # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def _find_client():
        # Host Hub -> requests; on-device -> urequests. Lazy, so neither is a dependency of a
        # deployment that is managed from nowhere.
        try:
            import requests
            return requests
        except ImportError:
            import urequests
            return urequests

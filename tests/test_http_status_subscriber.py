"""HTTP_Status_Subscriber: the live transfer, seen from somewhere other than the gateway.

A Hub knows the name of the file arriving, how many chunks are left and what the link is
doing, and until now the only way to read any of it was a terminal on the box. This subscriber
posts that to a web service, and everything below exists because attaching it to a running Hub
must be free.

Two properties are load-bearing and are pinned harder than the payload:

  * `update()` is called from inside the drive loop, once per chunk, between two radio
    exchanges. It must never post, never raise, and never hand out a reference into the live
    status dict.
  * A snapshot that could not be sent is dropped, not queued. This is the opposite of a
    DataSink's contract, and on purpose: a progress report is worth nothing once a newer one
    exists, so a slow site must not build a backlog of stale ones.
  * The last snapshot is repeated on the interval when nothing new arrives, so that a receiver
    can tell "the gateway is there and idle" from "the gateway is gone". A Hub between visits
    notifies nothing for a minute, which would otherwise look identical to a Hub that died.
"""
import json

from AlLoRa.Subscribers.HTTP_Status_Subscriber import HTTP_Status_Subscriber

SITE_URL = "https://control.example/api/hub/status"
TOKEN = "bench-token"


class _FakeResponse:
    def __init__(self, status_code, client, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._client = client

    def close(self):
        self._client.closed += 1


class _HeaderlessResponse:
    """Some urequests builds hand back a response with no `headers` at all. Reading one must
    not be the difference between a gateway that reports and a gateway that crashes."""

    def __init__(self, status_code):
        self.status_code = status_code


class _FakeHTTPClient:
    """Stand-in for `requests` / `urequests`, the way the sink's tests use one: both libraries
    are modules exposing the single call this makes."""

    def __init__(self, status_code=204, response_headers=None):
        self.status_code = status_code
        self.response_headers = response_headers or {}
        self.calls = []       # (url, decoded body, headers)
        self.closed = 0

    def post(self, url, data=None, headers=None, **kwargs):
        self.calls.append((url, json.loads(data), dict(headers or {})))
        return _FakeResponse(self.status_code, self, self.response_headers)


class _HeaderlessHTTPClient(_FakeHTTPClient):
    def post(self, url, data=None, headers=None, **kwargs):
        self.calls.append((url, json.loads(data), dict(headers or {})))
        return _HeaderlessResponse(self.status_code)


class _DeadHTTPClient:
    def post(self, *args, **kwargs):
        raise OSError("connection refused")


def _status(**overrides):
    """A Hub's status dict mid-transfer, with the keys the node actually writes."""
    values = {
        "MAC": "00000000", "SMAC": "da5a17b4", "Status": "REQUEST_DATA",
        "File": "-", "Chunk": 32,
        "RSSI": -71, "SNR": 9,
        "SF": 7, "BW": 125, "CR": 1, "TX_P": 14, "Freq": 868,
        "CorruptedPackets": 0, "Retransmission": 2,
        "Digital_Endpoints": {
            "da5a17b4": {"current_receiving_file_name": "260908-230000.xz",
                         "total_chunks": 248, "latest_chunk_index": 216},
        },
    }
    values.update(overrides)
    return values


class _Clock:
    """A clock the test moves by hand, injected the way the client is.

    Every rule in this class is about how much time has passed, and the backoff reaches five
    minutes. Waiting that out for real would be a test nobody runs, and sleeping for it would
    make the suite slower than the thing it tests.
    """

    def __init__(self, at=1_700_000_000_000):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, ms):
        self.at += ms


def _sub(client, **kwargs):
    return HTTP_Status_Subscriber(url=SITE_URL, token=TOKEN, client=client, **kwargs)


# -- what must not happen in the radio loop --------------------------------------------------


def test_update_does_not_post():
    """The single most important line here. `update()` runs between two radio exchanges, so a
    request inside it would put the site's latency into the transfer, and a site that stopped
    answering would stall every chunk for an HTTP timeout."""
    client = _FakeHTTPClient()
    sub = _sub(client)

    for _ in range(50):
        sub.update(_status())

    assert client.calls == []


def test_update_never_raises():
    """A subscriber that throws comes out of notify() inside the drive loop. Reporting is never
    worth a transfer, so even a status dict of the wrong shape is swallowed."""
    sub = _sub(_FakeHTTPClient())
    sub.update({"Digital_Endpoints": "not a dict at all"})
    sub.update(None)


def test_the_snapshot_is_a_copy_not_a_reference():
    """The node mutates its status dict and the endpoint's reception info in place, so a
    reference kept here would be read by the worker halfway through the next chunk and report
    a mix of two moments."""
    client = _FakeHTTPClient()
    sub = _sub(client)
    status = _status()

    sub.update(status)
    status["Chunk"] = 1
    status["Digital_Endpoints"]["da5a17b4"]["current_receiving_file_name"] = "something-else"
    sub._tick()

    _, body, _ = client.calls[0]
    assert body["chunks_left"] == 32
    assert body["file"] == "260908-230000.xz"


# -- the payload -----------------------------------------------------------------------------


def test_the_snapshot_carries_what_a_person_is_waiting_for():
    client = _FakeHTTPClient()
    sub = _sub(client)

    sub.update(_status())
    sub._tick()

    url, body, headers = client.calls[0]
    assert url == SITE_URL
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert headers["Content-Type"] == "application/json"
    assert body["node"] == "da5a17b4"
    assert body["file"] == "260908-230000.xz"
    assert body["chunks_total"] == 248
    assert body["chunks_left"] == 32
    assert body["rssi"] == -71 and body["snr"] == 9
    assert body["sf"] == 7 and body["bw"] == 125 and body["freq"] == 868
    assert body["retransmissions"] == 2
    assert isinstance(body["at"], int)


def test_a_field_the_node_has_not_measured_is_absent_rather_than_a_dash():
    """The node writes "-" into every live field at startup. Passing that through would make
    every reader downstream special-case a string that looks like data."""
    client = _FakeHTTPClient()
    sub = _sub(client)

    sub.update(_status(RSSI="-", SNR="-", SMAC="-", Status="-"))
    sub._tick()

    _, body, _ = client.calls[0]
    assert body["rssi"] is None and body["snr"] is None
    assert body["node"] is None and body["state"] is None


def test_a_serving_node_names_its_file_flat():
    """The same class on an Edge: the send side writes `File` and has no endpoint to read."""
    client = _FakeHTTPClient()
    sub = _sub(client)

    sub.update(_status(File="capture.xz", Digital_Endpoints={}, SMAC="b2b2b2b2"))
    sub._tick()

    _, body, _ = client.calls[0]
    assert body["file"] == "capture.xz"


def test_done_is_passed_through_rather_than_turned_into_a_number():
    """Inventing 0 for DONE would make a finished transfer indistinguishable from one with
    nothing left to ask for."""
    client = _FakeHTTPClient()
    sub = _sub(client)

    sub.update(_status(Chunk="DONE"))
    sub._tick()

    assert client.calls[0][1]["chunks_left"] == "DONE"


# -- newest wins, and the interval -----------------------------------------------------------


def test_only_the_newest_snapshot_is_kept():
    """Three chunks arrive between two ticks. The site hears about the third, and never about
    the first two, because they were already wrong."""
    client = _FakeHTTPClient()
    sub = _sub(client)

    sub.update(_status(Chunk=30))
    sub.update(_status(Chunk=29))
    sub.update(_status(Chunk=28))
    sub._tick()

    assert len(client.calls) == 1
    assert client.calls[0][1]["chunks_left"] == 28


def test_nothing_new_inside_the_interval_sends_nothing():
    """Inside the interval a tick with no news is silent. Past it the heartbeat below takes
    over, which is a different rule and tested as one."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status())
    sub._tick()
    sub._tick()
    sub._tick()

    assert len(client.calls) == 1


def test_ordinary_progress_is_throttled_to_the_interval():
    """Chunk counts change every tick. Without this the interval would mean nothing and a
    2460-chunk capture would be 2460 requests."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status(Chunk=32))
    sub._tick()
    sub.update(_status(Chunk=31))
    sub._tick()
    sub.update(_status(Chunk=30))
    sub._tick()

    assert len(client.calls) == 1


def test_a_new_file_is_sent_at_once_whatever_the_interval():
    """The moments a person watching the page is actually waiting for do not queue behind a
    throttle meant for chunk counts."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status())
    sub._tick()
    sub.update(_status(Digital_Endpoints={
        "da5a17b4": {"current_receiving_file_name": "260909-000000.xz", "total_chunks": 300}}))
    sub._tick()

    assert [c[1]["file"] for c in client.calls] == ["260908-230000.xz", "260909-000000.xz"]


def test_a_finished_transfer_is_sent_at_once():
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status(Chunk=32))
    sub._tick()
    sub.update(_status(Chunk="DONE"))
    sub._tick()

    assert [c[1]["chunks_left"] for c in client.calls] == [32, "DONE"]


# -- the heartbeat ---------------------------------------------------------------------------


def test_the_last_snapshot_is_repeated_while_nothing_happens():
    """A Hub between visits sleeps for its asking_frequency, a minute by default, and notifies
    nothing while it does. Without this the site would expire the gateway every minute and a
    page would show it appearing and vanishing, which says "the Hub is gone" when the truth is
    "the Hub has nothing to say"."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=0)

    sub.update(_status())
    sub._tick()          # the real one
    sub._tick()          # no news
    sub._tick()          # still no news

    assert len(client.calls) == 3
    assert {c[1]["file"] for c in client.calls} == {"260908-230000.xz"}


def test_the_heartbeat_invents_nothing():
    """The repeat is the same values again, the node's own `at` included. What makes it current
    is the receiver's clock, which is the only one either end should trust for that."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=0)

    sub.update(_status())
    sub._tick()
    sub._tick()

    assert client.calls[0][1] == client.calls[1][1]


def test_nothing_is_repeated_before_anything_was_sent():
    """A subscriber that has never had a snapshot has nothing to say, and must not post an
    empty one to prove it is alive."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=0)

    sub._tick()
    sub._tick()

    assert client.calls == []


def test_the_heartbeat_respects_the_interval():
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status())
    sub._tick()
    sub._tick()
    sub._tick()

    assert len(client.calls) == 1


# -- the site is down ------------------------------------------------------------------------


def test_a_refusal_is_swallowed():
    """A node's job is to move files. A status service that is down, or wrong about its token,
    must not be something the transfer notices."""
    client = _FakeHTTPClient(status_code=503)
    sub = _sub(client)

    sub.update(_status())
    sub._tick()

    assert len(client.calls) == 1        # tried


def test_an_unreachable_site_is_swallowed():
    sub = _sub(_DeadHTTPClient())
    sub.update(_status())
    sub._tick()                          # must not raise


def test_a_refused_transition_is_sent_again_rather_than_forgotten():
    """A refusal must not count as delivered: the site never heard that this file started, so
    a later snapshot has to carry it again. Later, and not at the next tick: the difference
    between a retry and a flood is entirely in when."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=503)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    client.status_code = 204
    sub.update(_status(Chunk=31))
    sub._tick()
    assert len(client.calls) == 1, "a refused transition must not go again immediately"

    clock.advance(60_000)
    sub.update(_status(Chunk=31))
    sub._tick()

    assert len(client.calls) == 2
    assert client.calls[1][1]["file"] == "260908-230000.xz"


def test_a_refused_post_is_not_retried_before_the_interval():
    """The runaway. A refusal left the last-sent clock untouched, so the next snapshot still
    looked like the first one about this file and went out at the worker's own tick rate. One
    gateway talking to a site that answered 500 spent a month of the database's allowance in
    three days. A refusal costs the interval, exactly like a success."""
    client = _FakeHTTPClient(status_code=500)
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status(Chunk=32))
    sub._tick()
    sub.update(_status(Chunk=31))
    sub._tick()
    sub.update(_status(Chunk=30))
    sub._tick()

    assert len(client.calls) == 1


def _wait_before_it_posts_again(sub, client, clock, step_ms=1_000, give_up_ms=900_000):
    """How long the subscriber makes us wait before it posts again, in milliseconds.

    Measured the way the worker experiences it, by ticking and moving the clock, rather than
    by reading a counter off the object. What matters is when a request leaves, not how the
    class arrived at the number.
    """
    before = len(client.calls)
    waited = 0
    while waited <= give_up_ms:
        sub.update(_status(Chunk=31))
        sub._tick()
        if len(client.calls) > before:
            return waited
        clock.advance(step_ms)
        waited += step_ms
    raise AssertionError("it never posted again")


def test_each_refusal_doubles_the_wait():
    """A site that is down is usually down for a while. Retrying it at a fixed interval is
    still tens of thousands of requests a day, every one of them refused, which is how a
    month of database allowance went in three days."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=500)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    assert len(client.calls) == 1

    waits = [_wait_before_it_posts_again(sub, client, clock) for _ in range(4)]

    assert waits == [20_000, 40_000, 80_000, 160_000]


def test_the_backoff_stops_at_five_minutes():
    """Doubling without a ceiling would eventually take a Hub off the page for hours, and the
    gateway would look dead to anyone watching long after the site came back."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=500)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    for _ in range(6):
        _wait_before_it_posts_again(sub, client, clock, step_ms=10_000)

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 300_000


def test_one_answer_clears_the_backoff():
    """The site coming back is the common case, and it must not be punished for the outage it
    just ended."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=500)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    for _ in range(3):
        _wait_before_it_posts_again(sub, client, clock)

    client.status_code = 204
    _wait_before_it_posts_again(sub, client, clock)          # the one that gets through

    assert _wait_before_it_posts_again(sub, client, clock) == 10_000


def test_a_transition_does_not_jump_the_backoff():
    """A new file is urgent enough to skip the interval. It is not urgent enough to skip a
    refusal: the site said no, and a new file does not change its mind."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=500)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    clock.advance(5_000)
    sub.update(_status(Digital_Endpoints={
        "da5a17b4": {"current_receiving_file_name": "260909-000000.xz", "total_chunks": 300}}))
    sub._tick()

    assert len(client.calls) == 1


def test_the_heartbeat_backs_off_too():
    """The repeat is a post like any other, and a Hub between visits is mostly repeats. A
    backoff that only covered new snapshots would leave an idle gateway hammering a site that
    is refusing it, which is most of a day for a Hub that visits once a minute."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=204)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()                       # gets through, so now there is something to repeat
    client.status_code = 500

    clock.advance(10_000)
    sub._tick()                       # the heartbeat, refused
    assert len(client.calls) == 2

    clock.advance(10_000)
    sub._tick()                       # inside the doubled wait: silent
    assert len(client.calls) == 2, "a refused heartbeat must cost more than the interval"

    clock.advance(10_000)
    sub._tick()                       # twenty seconds after the refusal: allowed again
    assert len(client.calls) == 3


def test_the_site_may_name_the_wait_itself():
    """A 429 with Retry-After is the site saying how long to stay away. Guessing our own
    number instead only asks it to say the same thing again, and the number it names is the
    one its rate limit actually uses."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=429, response_headers={"Retry-After": "120"})
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 120_000


def test_a_503_may_name_the_wait_as_well():
    """The site answers 503 with Retry-After when its own database is refusing it, which is
    the shape the outage actually had."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=503, response_headers={"retry-after": "300"})
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 300_000


def test_the_site_naming_a_date_falls_back_to_the_backoff():
    """Retry-After is allowed to be an HTTP date. Parsing one needs a date library the board
    does not have, so the honest answer is our own wait, not a crash and not zero."""
    clock = _Clock()
    client = _FakeHTTPClient(
        status_code=503, response_headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 20_000


def test_a_retry_after_on_any_other_refusal_is_ignored():
    """500 is the site being broken, not the site asking for time. Only the two codes that
    mean "not now" get to set the clock."""
    clock = _Clock()
    client = _FakeHTTPClient(status_code=500, response_headers={"Retry-After": "120"})
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 20_000


def test_the_wait_the_site_named_is_forgotten_once_it_answers():
    clock = _Clock()
    client = _FakeHTTPClient(status_code=429, response_headers={"Retry-After": "120"})
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()
    _wait_before_it_posts_again(sub, client, clock, step_ms=10_000)
    client.status_code = 204
    _wait_before_it_posts_again(sub, client, clock, step_ms=10_000)

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 10_000


def test_a_response_without_headers_is_not_an_error():
    """Not every urequests build exposes them. The backoff is the fallback, and the transfer
    never hears about any of it."""
    clock = _Clock()
    client = _HeaderlessHTTPClient(status_code=503)
    sub = _sub(client, interval_ms=10_000, now_ms=clock)

    sub.update(_status())
    sub._tick()

    assert _wait_before_it_posts_again(sub, client, clock, step_ms=10_000) == 20_000


def test_by_default_ordinary_progress_goes_out_every_thirty_seconds():
    """Two seconds was one Hub posting 43,200 times a day, which is a free database's month of
    allowance in a week even when every post succeeds. Thirty is 2,880, an order of magnitude
    inside every limit involved, and it costs a watcher nothing: a new file or a finished one
    still goes the moment the worker sees it. Only the chunk countdown slows down."""
    clock = _Clock()
    client = _FakeHTTPClient()
    sub = _sub(client, now_ms=clock)          # no interval: whatever a deployment gets by default

    sub.update(_status(Chunk=32))
    sub._tick()
    clock.advance(29_000)
    sub.update(_status(Chunk=31))
    sub._tick()
    assert len(client.calls) == 1

    clock.advance(1_000)
    sub.update(_status(Chunk=30))
    sub._tick()
    assert len(client.calls) == 2


def test_the_response_is_always_closed():
    """This posts far more often than the sink does, and urequests holds the socket until the
    response is closed."""
    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=0)

    for chunk in (32, 31, 30):
        sub.update(_status(Chunk=chunk))
        sub._tick()

    assert client.closed == 3


# -- construction ----------------------------------------------------------------------------


def test_a_subscriber_without_a_url_is_refused_at_construction():
    """The same rule HTTP_DataSink follows: one built without a destination would tick forever
    and report nothing, which is worse than refusing to start."""
    try:
        HTTP_Status_Subscriber(url=None)
    except ValueError:
        return
    raise AssertionError("a subscriber with nowhere to post should not be constructible")


# -- the worker ------------------------------------------------------------------------------


def test_start_runs_the_worker_and_stop_ends_it():
    """The one test that uses the real thread, because `start()` is the only place `_thread`
    is touched and a typo there would only show up on a gateway."""
    import time

    client = _FakeHTTPClient()
    sub = _sub(client, interval_ms=0)
    sub.start()
    try:
        sub.update(_status())
        deadline = time.time() + 5
        while not client.calls and time.time() < deadline:
            time.sleep(0.05)
        assert client.calls, "the worker never posted the snapshot"
    finally:
        sub.stop()

    time.sleep(0.5)
    before = len(client.calls)
    sub.update(_status(Chunk=1))
    time.sleep(0.5)
    assert len(client.calls) == before, "the worker kept posting after stop()"


def test_start_is_idempotent():
    """A restarted node must not end up with two workers on one subscriber."""
    import time

    sub = _sub(_FakeHTTPClient(), interval_ms=0)
    sub.start()
    sub.start()
    try:
        time.sleep(0.3)
    finally:
        sub.stop()
        time.sleep(0.5)

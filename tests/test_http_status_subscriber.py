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
    def __init__(self, status_code, client):
        self.status_code = status_code
        self._client = client

    def close(self):
        self._client.closed += 1


class _FakeHTTPClient:
    """Stand-in for `requests` / `urequests`, the way the sink's tests use one: both libraries
    are modules exposing the single call this makes."""

    def __init__(self, status_code=204):
        self.status_code = status_code
        self.calls = []       # (url, decoded body, headers)
        self.closed = 0

    def post(self, url, data=None, headers=None, **kwargs):
        self.calls.append((url, json.loads(data), dict(headers or {})))
        return _FakeResponse(self.status_code, self)


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
    the next snapshot has to carry it again."""
    client = _FakeHTTPClient(status_code=503)
    sub = _sub(client, interval_ms=10_000)

    sub.update(_status())
    sub._tick()
    client.status_code = 204
    sub.update(_status(Chunk=31))
    sub._tick()

    assert len(client.calls) == 2
    assert client.calls[1][1]["file"] == "260908-230000.xz"


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

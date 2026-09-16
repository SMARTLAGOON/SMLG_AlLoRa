"""HTTP_Management_Source: a Hub asks a website what it should be running.

The transport half of the management plane. The boundary, the intent slot and the drain are
already in the library and were proven on real radio; what was missing is the class that
actually goes and asks, which was deliberately left until the site had a route to answer it.

Four properties are load-bearing and are pinned harder than the payload:

  * **The exchange never happens inside the radio loop on a host.** `check()` is called from
    `Hub._drain_management`, between two visits, and the loop's next act is to listen to an
    endpoint. A site that has stopped answering would otherwise put its whole timeout between
    two radio exchanges, over and over.
  * **Nothing is delivered, so nothing is retried.** A refused exchange costs one interval of
    staleness and the same wish is still the wish on the next one. There is no queue here, no
    acknowledgement and no bookkeeping, which is the opposite of the DataSink's contract.
  * **A refusal costs more than a success.** The same rule the status subscriber learned the
    expensive way: a gateway whose refused posts are free spends a free tier's month in days.
  * **The interval is the site's to set.** `poll_interval_s` rides in the answer, so a person
    changes how often a Hub asks from the page. The bounds on it are the site's own policy and
    are deliberately not repeated here.
"""
import json

from AlLoRa.Management.HTTP_Management_Source import HTTP_Management_Source
from AlLoRa.Management.Management_Source import Management_Source

SITE_URL = "https://control.example/api/hub/management"
TOKEN = "bench-token"

# What a Hub says it is running, in the shape `Hub.management_report()` builds.
REPORT = {
    "paused": False,
    "nodes": {"a1b2": {"name": "T", "active": True, "wait_after_visit": 60,
                       "listening_time": 30, "lock_on_file_receive": True,
                       "max_listen_time_when_locked": 120, "stall_timeout": 30}},
    "one_shots": {},
    "config_file": "AlLoRa.json",
    "nodes_file": "Nodes.json",
}

# What the site answers with, in the library's own key names.
INTENT = {
    "paused": False,
    "nodes": {"a1b2": {"active": True, "name": "T", "wait_after_visit": 10}},
    "one_shots": {"reset_adapter": 7},
    "poll_interval_s": 30,
}


class _FakeResponse:
    def __init__(self, status_code, body, client, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self._client = client

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return json.loads(self._body)

    @property
    def text(self):
        return self._body

    def close(self):
        self._client.closed += 1


class _FakeHTTPClient:
    """Stand-in for `requests` / `urequests`, as the sink's and subscriber's tests use one."""

    def __init__(self, status_code=200, body=None, response_headers=None):
        self.status_code = status_code
        self.body = json.dumps(INTENT) if body is None else body
        self.response_headers = response_headers or {}
        self.calls = []       # (url, decoded body, headers)
        self.closed = 0

    def post(self, url, data=None, headers=None, **kwargs):
        self.calls.append((url, json.loads(data), dict(headers or {})))
        return _FakeResponse(self.status_code, self.body, self, self.response_headers)


class _DeadHTTPClient:
    def __init__(self):
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(args)
        raise OSError("connection refused")


class _Clock:
    """A hand-wound millisecond clock, so a test of a five minute backoff runs instantly."""

    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


def _source(client, clock=None, **kwargs):
    """A source pumped by the loop rather than by a worker, which is the board's arrangement
    and the one that makes an exchange happen at a point a test can name."""
    kwargs.setdefault("interval_s", 30)
    source = HTTP_Management_Source(url=SITE_URL, token=TOKEN, client=client,
                                    threaded=False, now_ms=clock or _Clock(), **kwargs)
    source.prepare()
    return source


# --- what it is -------------------------------------------------------------------------

def test_it_is_a_management_source():
    """A transport subclass, so a Hub takes it through the same verbs as any other filler of
    the slot and the base's drain works unchanged."""
    assert issubclass(HTTP_Management_Source, Management_Source)


def test_it_refuses_to_be_built_without_a_url():
    """Fails closed at construction, as HTTP_DataSink and the status subscriber do. There is
    no sensible default for which website owns a deployment, and a source built without one
    would poll forever and learn nothing."""
    try:
        HTTP_Management_Source(url=None, token=TOKEN)
    except ValueError:
        return
    raise AssertionError("a source with nowhere to ask should not be constructible")


# --- the exchange -----------------------------------------------------------------------

def test_it_says_what_is_running_and_takes_back_what_is_wanted():
    """One request, both directions. The Hub's report is the body and the answer is the wish,
    which is why the boundary never needs a reference back to the node it serves."""
    client = _FakeHTTPClient()
    source = _source(client)
    source.report(REPORT)
    source.check()

    assert len(client.calls) == 1
    url, body, headers = client.calls[0]
    assert url == SITE_URL
    assert body == REPORT
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert headers["Content-Type"] == "application/json"
    assert source.has_intent()
    assert source.take_intent() == INTENT


def test_it_says_nothing_until_the_hub_has_reported():
    """The site's route refuses a body that is not a report, so an exchange before the Hub has
    said anything is a request that can only be answered with a 400. A source brought up before
    the first visit waits instead of spending one."""
    client = _FakeHTTPClient()
    source = _source(client)
    source.check()
    assert client.calls == []


def test_the_slot_is_emptied_by_the_take():
    """The drain takes the wish and hands it to the Hub. Taking it twice must not apply the
    same roster change twice, and a one-shot counter read twice is the fault that whole
    mechanism exists to prevent."""
    source = _source(_FakeHTTPClient())
    source.report(REPORT)
    source.check()

    assert source.take_intent() == INTENT
    assert not source.has_intent()
    assert source.take_intent() is None


def test_the_newest_wish_replaces_the_one_before_it():
    """Latest wins, and this is state rather than a queue of events. A Hub that was busy for
    two intervals applies what is wanted now, not what was wanted at the start of its visit."""
    client = _FakeHTTPClient()
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()

    client.body = json.dumps({"paused": True, "nodes": {}, "one_shots": {},
                              "poll_interval_s": 30})
    clock.advance(30_000)
    source.check()

    assert source.take_intent()["paused"] is True
    assert not source.has_intent()


def test_the_response_is_closed():
    """urequests holds the socket until the response is closed, and a Hub asking every thirty
    seconds for a year is a million of them."""
    client = _FakeHTTPClient()
    source = _source(client)
    source.report(REPORT)
    source.check()
    assert client.closed == 1


# --- staying out of the radio loop -------------------------------------------------------

def test_a_worker_takes_the_asking_off_the_loop():
    """On a host the exchange belongs to a worker thread, so that `check()` returns at once
    however long the site takes. This is the property that makes the boundary safe to attach to
    a running gateway, and it is checked by watching the exchange happen without `check()`."""
    import time as _time

    client = _FakeHTTPClient()
    source = HTTP_Management_Source(url=SITE_URL, token=TOKEN, client=client,
                                    interval_s=0, threaded=True)
    source.prepare()
    try:
        source.report(REPORT)
        deadline = _time.time() + 5
        while not client.calls and _time.time() < deadline:
            _time.sleep(0.01)
        assert client.calls, "the worker never performed the exchange"
    finally:
        source.close()
    assert source.is_running() is False


def test_close_is_idempotent():
    """A node brought down twice, or a source replaced by another, must not leave a thread
    behind or raise on the second call."""
    source = _source(_FakeHTTPClient())
    source.close()
    source.close()
    assert source.is_running() is False


# --- what it costs ------------------------------------------------------------------------

def test_it_does_not_ask_more_often_than_the_interval():
    """`check()` is called between every visit, so an ungated source on a Hub holding eight
    endpoints would ask eight times a pass. The interval is what keeps one gateway's management
    channel to a number a free tier can carry."""
    client = _FakeHTTPClient()
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()
    assert len(client.calls) == 1

    clock.advance(29_000)
    source.check()
    assert len(client.calls) == 1

    clock.advance(1_000)
    source.check()
    assert len(client.calls) == 2


def test_the_site_sets_the_interval():
    """How often a Hub asks is desired state like everything else, so a person changes it on
    the page. Read here rather than in the Hub because it is the boundary's own business: the
    Hub applies a roster and does not care how the wish arrived."""
    client = _FakeHTTPClient(body=json.dumps(dict(INTENT, poll_interval_s=300)))
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()

    clock.advance(30_000)
    source.check()
    assert len(client.calls) == 1, "the interval the site asked for was ignored"

    clock.advance(270_000)
    source.check()
    assert len(client.calls) == 2


def test_an_unusable_interval_leaves_the_current_one_alone():
    """The bounds on this are the site's policy and are deliberately not repeated here, so the
    only thing guarded is what would break the loop: a value that is not a positive number
    would otherwise turn the management channel into a spin."""
    client = _FakeHTTPClient(body=json.dumps(dict(INTENT, poll_interval_s=0)))
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()

    source.check()
    assert len(client.calls) == 1
    clock.advance(30_000)
    source.check()
    assert len(client.calls) == 2


def test_a_refusal_costs_more_than_a_success():
    """A run of refusals doubles the wait. A gateway whose refused posts were free once spent a
    month of a free database's allowance in three days, against a site answering every one of
    them with an error."""
    client = _FakeHTTPClient(status_code=500, body="nope")
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()
    assert len(client.calls) == 1

    clock.advance(30_000)
    source.check()
    assert len(client.calls) == 1, "a refused exchange was retried at the ordinary interval"

    clock.advance(30_000)
    source.check()
    assert len(client.calls) == 2


def test_a_delivered_answer_clears_the_backoff():
    """A site that has come back is asked at the ordinary interval again, rather than serving
    out a wait earned while it was down."""
    client = _FakeHTTPClient(status_code=503, body="nope")
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()

    client.status_code = 200
    client.body = json.dumps(INTENT)
    clock.advance(60_000)
    source.check()
    assert len(client.calls) == 2

    clock.advance(30_000)
    source.check()
    assert len(client.calls) == 3


def test_the_wait_the_site_asks_for_outranks_our_own():
    """A site naming a number is the one enforcing the limit and knows when it will lift."""
    client = _FakeHTTPClient(status_code=429, body="slow down",
                             response_headers={"Retry-After": "120"})
    clock = _Clock()
    source = _source(client, clock=clock)
    source.report(REPORT)
    source.check()

    clock.advance(60_000)
    source.check()
    assert len(client.calls) == 1

    clock.advance(60_000)
    source.check()
    assert len(client.calls) == 2


# --- never at the transfer's expense -------------------------------------------------------

def test_a_site_that_cannot_be_reached_is_not_an_error():
    """A node's job is to move files. If the site is down, or wrong about its token, or gone,
    the visit loop must not notice: `check()` is called between two radio exchanges."""
    client = _DeadHTTPClient()
    source = _source(client)
    source.report(REPORT)
    source.check()
    assert not source.has_intent()


def test_a_refused_exchange_leaves_the_slot_alone():
    """A refusal is an attempt, not news. The wish the Hub already holds is still the wish, and
    an error page parsed as a roster is how a gateway would be told to disable every node."""
    client = _FakeHTTPClient(status_code=500, body="<html>gateway timeout</html>")
    source = _source(client)
    source.report(REPORT)
    source.check()
    assert not source.has_intent()


def test_an_answer_that_is_not_a_wish_is_not_applied():
    """A 200 carrying something that is not a JSON object. A proxy's login page, a truncated
    body, or a site half way through a deploy all arrive this way, and `Hub._apply_intent`
    reads the object directly."""
    for body in ("<html>sign in</html>", "null", "[1, 2, 3]", '"paused"'):
        client = _FakeHTTPClient(body=body)
        source = _source(client)
        source.report(REPORT)
        source.check()
        assert not source.has_intent(), "{!r} was taken for a wish".format(body)

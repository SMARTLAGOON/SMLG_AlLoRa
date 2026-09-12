"""A Hub is told what it should be running, and reports what it is.

The library half of the management plane. Nothing here crosses the LoRa link: `active`, the
timing values and a node's name are read by the Hub alone, out of the `Nodes.json` entry
behind each `Digital_Endpoint`, and an Edge never learns any of them. That is the whole reason
this feature is not blocked on a downlink, and it is why the boundary exists on a Hub and on
nothing else.

Three things are pinned here.

**A change lands between visits, at the cost of at most one listening window.** Not mid-visit,
which would cost the exchange in flight, and not once a round, which on a Hub holding several
Edges is minutes rather than seconds.

**Nothing is delivered, so nothing needs a delivery guarantee.** The slot holds the newest
desired state and nothing else: setting a value twice replaces it, there is no queue, and a
change that gets lost simply keeps not matching and goes out again. The one exception is an
imperative, which does not reconcile, so it is not modelled as one: a generation number gives
at-most-once by construction and survives a reboot.

**The endpoints already held are never rebuilt.** An endpoint object carries the reassembly in
progress; a timing value changes on the object that is already there, or a transfer part way
through is thrown away to apply a number.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Management.Management_Source import Management_Source
from AlLoRa.Nodes import Hub as hub_module
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"
_TICKS_PERIOD = 1 << 30


def _write_config(path, result_path, **extra):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False,
        "protocol_version": 3, "security_mode": "open", "session_id": 9,
        "debug": False, "result_path": result_path,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    config.update(extra)
    with open(path, "w") as f:
        json.dump(config, f)


def _node(name, mac, **overrides):
    node = {"name": name, "mac_address": mac, "active": True,
            "asking_frequency": 60, "listening_time": 30}
    node.update(overrides)
    return node


def _make_hub(tmp_path, nodes=None, **kwargs):
    """A Hub whose management state has somewhere of its own to live, per test."""
    config = str(tmp_path / "hub.json")
    _write_config(config, str(tmp_path / "results"),
                  management_state_file=str(tmp_path / "management.state"))
    if nodes is not None:
        nodes_file = str(tmp_path / "Nodes.json")
        with open(nodes_file, "w") as f:
            json.dump(nodes, f)
        kwargs.setdefault("nodes_file", nodes_file)
    return Hub(Loopback_connector(HUB_MAC), config_file=config, **kwargs)


def _roster(hub):
    with open(hub.nodes_file, "r") as f:
        return json.load(f)


def _entry(hub, mac):
    return next(e for e in _roster(hub) if e["mac_address"] == mac)


class _Clock:
    """A millisecond counter that wraps exactly like MicroPython's `ticks_ms`."""

    def __init__(self, start=0):
        self.now = start % _TICKS_PERIOD

    def __call__(self):
        return self.now

    def advance_ms(self, ms):
        self.now = (self.now + int(ms)) % _TICKS_PERIOD


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(hub_module, "time", c)
    monkeypatch.setattr(hub_module, "sleep", lambda s: c.advance_ms(max(1, s * 1000)))
    return c


def _record_visits(hub, clock, visits, on_visit=None):
    def listen(digital_endpoint, listening_time=None, print_file=False, save_file=False,
               one_file=False, stall_timeout=None):
        visits.append((digital_endpoint.get_name(), listening_time))
        clock.advance_ms((listening_time or 0) * 1000)
        if on_visit is not None:
            on_visit(digital_endpoint)
        return True

    hub.listen_to_endpoint = listen


# --- the boundary ---------------------------------------------------------------------------

def test_the_slot_holds_the_newest_wish_and_not_a_queue_of_them():
    # Desired state, not events. Setting a value twice replaces it; there is nothing to
    # cancel except by setting it back, and nothing to drain in order.
    source = Management_Source()

    source.submit_intent({"paused": True})
    source.submit_intent({"paused": False})

    assert source.take_intent() == {"paused": False}
    assert source.take_intent() is None


def test_a_source_is_told_what_the_hub_runs_before_it_is_asked_what_it_should():
    # One exchange, in that order: the Hub reports what it is running, and the answer is the
    # desired state. A source that had to ask the Hub for its report would need a reference
    # back to the node, which is the coupling every other boundary in the library avoids.
    source = Management_Source()

    source.report({"paused": False})

    assert source.last_report() == {"paused": False}


def test_only_a_hub_holds_this_boundary():
    # The trust rule, and the reason it is a rule rather than something the class name carries.
    # A node holding a control root refuses unsigned in-band control from then on, so that the
    # signature is protecting something; an Edge holding this boundary would be a second,
    # unsigned way to change that node's settings.
    from AlLoRa.Nodes.Edge import Edge

    assert hasattr(Hub, "set_management_source")
    assert not hasattr(Edge, "set_management_source")
    assert not hasattr(Edge, "submit_intent")


# --- the drain: when a change lands ---------------------------------------------------------

def test_a_wish_submitted_mid_visit_is_applied_once_that_visit_ends(tmp_path, clock):
    # The boundary the design promises. Waiting means the visit in progress, not the file in
    # progress: one listening window is seconds to a few minutes, while a busy node can hold a
    # partial file for hours, and with several Edges a Hub may be part way through several.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=5,
                                     listening_time=1)])
    visits = []

    def pause_from_inside(_endpoint):
        if len(visits) == 1:
            hub.submit_intent({"paused": True})

    _record_visits(hub, clock, visits, on_visit=pause_from_inside)

    hub.run(timeout=60)

    # The visit that asked for the pause ran to its end, and nothing was polled after it.
    assert len(visits) == 1


def test_a_change_waits_for_one_window_and_not_for_the_whole_round(tmp_path, clock):
    # Draining once a pass instead of once a visit would make a three-endpoint Hub take three
    # listening windows to notice, which is the promise this test exists to hold to.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=30),
                               _node("edge-b", "b1b1b1b1", listening_time=30),
                               _node("edge-c", "c1c1c1c1", listening_time=30)])
    visits = []

    def pause_from_inside(_endpoint):
        if len(visits) == 1:
            hub.submit_intent({"paused": True})

    _record_visits(hub, clock, visits, on_visit=pause_from_inside)

    hub.run(timeout=600)

    assert len(visits) == 1, "the pause waited for the round rather than for the window"


def test_a_hub_with_nothing_to_poll_can_still_be_told_something(tmp_path, clock):
    # A fleet emptied by a disable, or one whose roster has not arrived yet, is otherwise
    # unreachable: the loop's idle branch would spin forever without ever reading the boundary.
    hub = _make_hub(tmp_path, [])
    source = Management_Source()
    hub.set_management_source(source)
    source.submit_intent({"paused": True})

    hub.run(timeout=5)

    assert hub.paused


# --- pause ----------------------------------------------------------------------------------

def test_pausing_stops_the_polling_and_resuming_starts_it_again(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=1,
                                     listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.submit_intent({"paused": True})
    hub.run(timeout=20)
    assert visits == []

    hub.submit_intent({"paused": False})
    hub.run(timeout=20)
    assert visits, "a resumed Hub polls again"


def test_a_paused_hub_comes_back_paused(tmp_path):
    # Coming back live while somebody has the antenna in their hand is the failure the button
    # exists to prevent, so the pause is remembered rather than reset by a power cycle.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    hub.submit_intent({"paused": True})
    hub._drain_management()

    rebooted = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])

    assert rebooted.paused


def test_a_live_hub_comes_back_live(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])

    assert hub.paused is False


# --- enable and disable ----------------------------------------------------------------------

def test_disabling_a_node_takes_it_out_of_the_loop(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("keeper", "a1a1a1a1", asking_frequency=1,
                                     listening_time=1),
                               _node("goner", "b1b1b1b1", asking_frequency=1,
                                     listening_time=1)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": False}}})
    hub.run(timeout=30)

    assert "goner" not in {name for name, _ in visits}
    assert "keeper" in {name for name, _ in visits}


def test_disabling_a_node_is_written_into_the_roster(tmp_path):
    # The Hub applies the change and writes its own file. A second writer would be a
    # lost-update race against the write-back a committed RF trial already performs.
    hub = _make_hub(tmp_path, [_node("goner", "b1b1b1b1")])

    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": False}}})
    hub._drain_management()

    assert _entry(hub, "b1b1b1b1")["active"] is False


def test_re_enabling_a_node_puts_it_back_in_the_loop(tmp_path, clock):
    # The other half of the pair of buttons, and the one that used to kill the loop.
    hub = _make_hub(tmp_path, [_node("keeper", "a1a1a1a1", asking_frequency=1,
                                     listening_time=1),
                               _node("sleeper", "b1b1b1b1", active=False,
                                     asking_frequency=1, listening_time=1)])
    assert [ep.get_name() for ep in hub.digital_endpoints] == ["keeper"]
    visits = []
    _record_visits(hub, clock, visits)

    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": True}}})
    hub.run(timeout=30)

    assert "sleeper" in {name for name, _ in visits}
    assert _entry(hub, "b1b1b1b1")["active"] is True


def test_a_node_disabled_and_re_enabled_is_registered_once(tmp_path):
    # Gaining a node runs through the same verb a roster re-read does, so it inherits that
    # verb's idempotence rather than growing a second registration path beside it.
    hub = _make_hub(tmp_path, [_node("flapper", "b1b1b1b1")])

    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": False}}})
    hub._drain_management()
    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": True}}})
    hub._drain_management()

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["flapper"]


# --- the timing values -------------------------------------------------------------------

def test_a_new_listening_time_is_used_by_the_very_next_visit(tmp_path, clock):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=1,
                                     listening_time=3)])
    visits = []
    _record_visits(hub, clock, visits)

    hub.submit_intent({"nodes": {"a1a1a1a1": {"listening_time": 9}}})
    hub.run(timeout=10)

    assert visits[0] == ("edge-a", 9)


def test_a_timing_change_does_not_rebuild_the_endpoint_that_holds_the_transfer(tmp_path):
    # An endpoint object carries the live half: the reassembly in progress and the state
    # machine driving it. On a slow link a part-received file is hours of airtime, so a number
    # is set on the object that is already there rather than applied by building a new one.
    class _PartialFile:
        def get_missing_chunks(self):
            return [7]

        def discard(self):
            raise AssertionError("a timing change must not discard the file in flight")

    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    endpoint = hub.digital_endpoints[0]
    in_flight = _PartialFile()
    endpoint.set_current_file(in_flight)

    hub.submit_intent({"nodes": {"a1a1a1a1": {"asking_frequency": 30}}})
    hub._drain_management()

    assert hub.digital_endpoints[0] is endpoint
    assert endpoint.get_current_file() is in_flight
    assert endpoint.asking_frequency == 30


def test_every_timing_value_is_applied_and_written_down(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    wish = {"asking_frequency": 15, "listening_time": 8,
            "lock_on_file_receive": True, "stall_timeout": 12,
            "max_listen_time_when_locked": 240}

    hub.submit_intent({"nodes": {"a1a1a1a1": wish}})
    hub._drain_management()

    endpoint = hub.digital_endpoints[0]
    for field, value in wish.items():
        assert getattr(endpoint, field) == value, field
    entry = _entry(hub, "a1a1a1a1")
    for field, value in wish.items():
        assert entry[field] == value, field


def test_the_locked_window_a_wish_sets_is_the_one_the_next_visit_grants(tmp_path, clock):
    # The field is only worth setting from outside if it bounds the extra window the lock
    # actually opens. A node left mid-file gets that window on the spot, so a wish that
    # changed the number and did not reach this call would be a control that says it did.
    class _PartialFile:
        def get_missing_chunks(self):
            return [7]

    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=1,
                                     listening_time=3, lock_on_file_receive=True,
                                     max_listen_time_when_locked=300)])
    hub.digital_endpoints[0].set_current_file(_PartialFile())
    visits = []
    _record_visits(hub, clock, visits)

    hub.submit_intent({"nodes": {"a1a1a1a1": {"max_listen_time_when_locked": 45}}})
    hub.run(timeout=10)

    assert visits[0] == ("edge-a", 3), "the ordinary visit is unchanged"
    assert visits[1] == ("edge-a", 45), "the locked window is the one the wish asked for"


def test_a_rename_is_applied_and_written_down(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])

    hub.submit_intent({"nodes": {"a1a1a1a1": {"name": "mast-north"}}})
    hub._drain_management()

    assert hub.digital_endpoints[0].get_name() == "mast-north"
    assert _entry(hub, "a1a1a1a1")["name"] == "mast-north"


def test_a_rename_moves_nothing_a_node_is_addressed_or_filed_by(tmp_path):
    # A name is for people. What the results folder, the MQTT topic and every wish are keyed
    # by is the label, derived from the device_id or the MAC, so renaming a node cannot orphan
    # a transfer in flight, move a folder, or make the next wish miss it.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", device_id="1ee385e641d52898")])
    endpoint = hub.digital_endpoints[0]
    label_before, session_before = endpoint.get_label(), endpoint.session_id

    hub.submit_intent({"nodes": {label_before: {"name": "mast-north"}}})
    hub._drain_management()

    assert hub.digital_endpoints[0] is endpoint
    assert endpoint.get_label() == label_before
    assert endpoint.session_id == session_before
    assert hub.management_report()["nodes"][label_before]["name"] == "mast-north"


def test_a_rename_of_a_node_this_hub_does_not_hold_is_still_written_down(tmp_path):
    # An inactive entry is no endpoint at all, and renaming one is the ordinary case of
    # labelling a node before it is switched on. The file is what the next boot believes.
    hub = _make_hub(tmp_path, [_node("sleeper", "b1b1b1b1", active=False)])

    hub.submit_intent({"nodes": {"b1b1b1b1": {"name": "mast-south"}}})
    hub._drain_management()

    assert _entry(hub, "b1b1b1b1")["name"] == "mast-south"
    assert hub.digital_endpoints == []


def test_the_rest_of_a_roster_entry_survives_the_write(tmp_path):
    # Overlaid, never rebuilt, for the same reason the RF write-back is: an entry carries keys
    # this node does not model, and rebuilding from the live endpoints would drop every one of
    # them and delete outright the inactive entries that never become endpoints at all.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", device_id="1ee385e641d52898",
                                     something_nobody_models=7),
                               _node("sleeper", "b1b1b1b1", active=False)])

    hub.submit_intent({"nodes": {"1ee385e6": {"listening_time": 5}}})
    hub._drain_management()

    roster = _roster(hub)
    assert len(roster) == 2, "the inactive entry must survive"
    assert roster[0]["something_nobody_models"] == 7
    assert roster[0]["device_id"] == "1ee385e641d52898"


def test_disabling_one_node_does_not_move_another_nodes_session(tmp_path):
    # Two identities sharing a first byte derive the same session id, so one of them is bumped
    # off it at registration: the case where a survivor's id is not the one its identity
    # implies, and so the case where re-deriving ids could move it. Everything this Hub holds
    # per endpoint is keyed by that id (a queued downlink, a live secure session, an RF trial
    # part way through), and none of it would announce having been orphaned; the node would
    # simply stop being answered for. Disabling a neighbour is not allowed to reach any of it.
    hub = _make_hub(tmp_path, [_node("first", "a1a1a1a1", device_id="aa11223344556677"),
                               _node("second", "b1b1b1b1", device_id="aa8899aabbccddee")])
    survivor = hub.digital_endpoints[1]
    sid_before = survivor.session_id

    hub.submit_intent({"nodes": {survivor.get_label(): {"active": True},
                                 hub.digital_endpoints[0].get_label(): {"active": False}}})
    hub._drain_management()

    assert [ep.get_name() for ep in hub.digital_endpoints] == ["second"]
    assert survivor.session_id == sid_before
    assert list(hub.status["Digital_Endpoints"]) == [survivor.get_label()]


def test_a_wish_about_a_node_this_hub_does_not_have_changes_nothing(tmp_path):
    # The site holds the wish for a fleet; a Hub answers only for its own. Silently ignoring
    # the rest is what lets one document describe several boxes.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    before = _roster(hub)

    hub.submit_intent({"nodes": {"ffffffff": {"active": False}}})
    hub._drain_management()

    assert _roster(hub) == before
    assert [ep.get_name() for ep in hub.digital_endpoints] == ["edge-a"]


# --- one-shots -------------------------------------------------------------------------------

def test_a_one_shot_runs_once_however_often_the_wish_is_read(tmp_path):
    # An imperative does not reconcile, so it is not modelled as one. The site says "reset
    # generation 7", the Hub has acted through 6, so it acts once and records 7.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    fired = []
    hub.register_one_shot("reset_adapter", lambda: fired.append(1))

    for _ in range(3):
        hub.submit_intent({"one_shots": {"reset_adapter": 7}})
        hub._drain_management()

    assert len(fired) == 1


def test_a_higher_generation_runs_it_again(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    fired = []
    hub.register_one_shot("reset_adapter", lambda: fired.append(1))

    hub.submit_intent({"one_shots": {"reset_adapter": 7}})
    hub._drain_management()
    hub.submit_intent({"one_shots": {"reset_adapter": 8}})
    hub._drain_management()

    assert len(fired) == 2


def test_a_one_shot_already_acted_on_is_not_repeated_after_a_reboot(tmp_path):
    # At-most-once has to survive a power cycle or it is not at-most-once. The precedent is
    # the control counter, which is persisted for the same reason.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    hub.register_one_shot("reset_adapter", lambda: None)
    hub.submit_intent({"one_shots": {"reset_adapter": 7}})
    hub._drain_management()

    rebooted = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    fired = []
    rebooted.register_one_shot("reset_adapter", lambda: fired.append(1))
    rebooted.submit_intent({"one_shots": {"reset_adapter": 7}})
    rebooted._drain_management()

    assert fired == []


def test_a_one_shot_nothing_on_this_box_can_do_is_not_recorded_as_done(tmp_path):
    # The self-healing half of reconciling. A wish nobody can satisfy keeps not matching, so
    # it stays visible instead of being quietly marked complete, and it runs the moment the
    # deployment registers something that can do it.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])

    hub.submit_intent({"one_shots": {"reset_adapter": 7}})
    hub._drain_management()

    assert hub.management_report()["one_shots"] == {}

    fired = []
    hub.register_one_shot("reset_adapter", lambda: fired.append(1))
    hub.submit_intent({"one_shots": {"reset_adapter": 7}})
    hub._drain_management()

    assert fired == [1]


# --- what the Hub reports ----------------------------------------------------------------------

def test_the_report_says_what_each_endpoint_is_actually_running(tmp_path):
    # Applied is an observation, not a message: the site does not wait for an acknowledgement,
    # it compares this against the wish it holds. That is what makes a lost change self-healing
    # with no bookkeeping, and it is what lets a second editor exist at all.
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=15,
                                     listening_time=8)])

    report = hub.management_report()

    assert report["paused"] is False
    running = report["nodes"]["a1a1a1a1"]
    assert running["name"] == "edge-a"
    assert running["active"] is True
    assert running["asking_frequency"] == 15
    assert running["listening_time"] == 8


def test_the_report_shows_a_change_the_hub_has_taken_up(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", listening_time=8)])

    hub.submit_intent({"paused": True, "nodes": {"a1a1a1a1": {"listening_time": 40}}})
    hub._drain_management()

    report = hub.management_report()
    assert report["paused"] is True
    assert report["nodes"]["a1a1a1a1"]["listening_time"] == 40


def test_a_disabled_node_is_absent_from_what_the_hub_reports_it_runs(tmp_path):
    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1"), _node("goner", "b1b1b1b1")])

    hub.submit_intent({"nodes": {"b1b1b1b1": {"active": False}}})
    hub._drain_management()

    assert list(hub.management_report()["nodes"]) == ["a1a1a1a1"]


# --- the second filler: a board with no thread ---------------------------------------------------

def test_the_loop_fills_the_slot_from_the_feed_when_no_thread_does(tmp_path, clock):
    # One slot, two fillers. A thread fills it on an SBC; on a microcontroller acting alone as
    # a Hub there is no API thread, so the loop pumps the feed itself between visits. The slot
    # and the drain are identical on both, which is the whole reason they are in the library.
    class _Feed(Management_Source):
        def __init__(self):
            Management_Source.__init__(self)
            self.polls = 0

        def check(self):
            self.polls += 1
            if self.polls == 1:
                self.submit_intent({"nodes": {"a1a1a1a1": {"listening_time": 4}}})

    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1", asking_frequency=1,
                                     listening_time=30)])
    feed = _Feed()
    hub.set_management_source(feed)
    visits = []
    _record_visits(hub, clock, visits)

    hub.run(timeout=60)

    assert visits[0] == ("edge-a", 4)
    assert feed.last_report() is not None, "the feed is handed the report to send with its poll"


def test_registering_a_source_brings_it_up_at_registration(tmp_path):
    # Same rule as the downlink source: a connect that must fail should fail at setup, loudly,
    # rather than part way through a drive loop.
    class _Fussy(Management_Source):
        def __init__(self):
            Management_Source.__init__(self)
            self.prepared = False

        def prepare(self):
            self.prepared = True

    hub = _make_hub(tmp_path, [_node("edge-a", "a1a1a1a1")])
    source = _Fussy()

    hub.set_management_source(source)

    assert source.prepared

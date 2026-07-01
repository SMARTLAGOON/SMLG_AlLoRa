"""Unit — Pacing, the adaptive receive-window controller (extracted from the Connector).

The window (v2's `adaptive_timeout`) starts wide and tightens toward the observed
round-trip on each good reply (EWMA smoothing 0.2), and jitter-grows back toward `max` on
a timeout — floored by the ToA `min` and the best round-trip seen, capped at `max`. This
logic lived scattered on the Connector with zero tests; extracting it into a pure,
radio-free policy object makes it unit-testable for the first time. These tests pin the
exact behavior so the extraction is provably zero-behavior.
"""
from AlLoRa.Pacing import Pacing


def test_window_starts_at_max():
    p = Pacing(min_timeout=0.5, max_timeout=6.0)
    assert p.window == 6.0


def test_set_bounds_resets_the_window_to_max():
    p = Pacing(min_timeout=0.5, max_timeout=6.0)
    p.on_reply(1.0)                 # move the window off max
    assert p.window < 6.0
    p.set_bounds(0.2, 4.0)          # an RF change
    assert p.min_timeout == 0.2 and p.max_timeout == 4.0
    assert p.window == 4.0          # window resets to the new max


def test_on_reply_tightens_the_window_toward_the_round_trip():
    p = Pacing(min_timeout=0.5, max_timeout=6.0)
    # EWMA: 6*0.8 + 1.0*0.2 = 5.0, floored by max(min=0.5, observed_min=1.0) = 1.0 -> 5.0
    p.on_reply(1.0)
    assert abs(p.window - 5.0) < 1e-9
    assert p.observed_min_timeout == 1.0


def test_on_reply_is_floored_by_the_best_round_trip_seen():
    p = Pacing(min_timeout=0.1, max_timeout=6.0)
    for _ in range(200):
        p.on_reply(2.0)             # converge downward toward 2.0...
    assert p.window >= 2.0          # ...but never below the best td seen
    assert abs(p.window - 2.0) < 1e-6


def test_on_timeout_grows_the_window_toward_max_never_past():
    p = Pacing(min_timeout=0.5, max_timeout=6.0)
    p.on_reply(1.0)                 # window ~5.0
    before = p.window
    p.on_timeout()
    assert before <= p.window <= 6.0   # jitter-grows within (window, max]


def test_window_is_bounded_by_min_timeout():
    p = Pacing(min_timeout=1.5, max_timeout=6.0)
    for _ in range(500):
        p.on_reply(0.01)            # tiny round-trips would drive it low...
    assert p.window >= 1.5          # ...but the ToA floor holds


# --- the inter-request sleep controller (folded in from the Requester's loop) --------------
# The Requester used to hunt for the shortest inter-request gap the link tolerates inline in
# `listen_to_endpoint`, with the heuristic never directly asserted by any test. These pin the
# exact behavior now that it lives in Pacing (`set_sleep_bounds` / `on_success` / `on_failure`
# / `next_sleep`), so the extraction is provably zero-behavior.


def test_sleep_starts_at_the_min_bound():
    p = Pacing()
    p.set_sleep_bounds(0.1, 3.0)
    assert p.sleep == 0.1
    assert p.next_sleep() == 0.1


def test_sleep_holds_until_enough_consecutive_successes():
    p = Pacing(successful_interactions_required=5)
    p.set_sleep_bounds(0.1, 3.0)
    for _ in range(4):
        p.on_success()              # four in a row is one short of the probe threshold
    assert p.sleep == 0.1
    assert p.successful_interactions_count == 4


def test_success_run_probes_a_shorter_sleep():
    p = Pacing(successful_interactions_required=5)
    p.set_sleep_bounds(0.1, 3.0)
    for _ in range(5):
        p.on_success()
    # the 5th success crosses the threshold -> EWMA-shrink 0.1*(1-0.2) = 0.08
    assert abs(p.sleep - 0.08) < 1e-9
    assert p.sleep_just_decreased is True


def test_probing_is_floored_at_the_absolute_minimum():
    p = Pacing(successful_interactions_required=1)
    p.set_sleep_bounds(0.1, 3.0)
    for _ in range(200):
        p.on_success()              # every success now shrinks the gap...
    assert abs(p.sleep - 0.01) < 1e-6   # ...but never below the 0.01 s absolute floor


def test_failure_below_threshold_doubles_the_sleep():
    p = Pacing(exponential_backoff_threshold=0.5)
    p.set_sleep_bounds(0.1, 3.0)    # 0.1 < 0.5 -> exponential back-off
    p.on_failure()
    assert abs(p.sleep - 0.2) < 1e-9
    assert p.failure_count == 1


def test_failure_above_threshold_grows_toward_max_never_past():
    p = Pacing(exponential_backoff_threshold=0.5, max_failures=3)
    p.set_sleep_bounds(2.9, 3.0)    # 2.9 >= 0.5 -> jittered growth, capped at max
    for _ in range(20):
        p.on_failure()
    assert p.sleep <= 3.0


def test_failed_probe_falls_back_and_pins_the_floor():
    p = Pacing(successful_interactions_required=5)
    p.set_sleep_bounds(0.1, 3.0)
    for _ in range(5):
        p.on_success()              # probe down to 0.08, remembering the pre-probe 0.1
    assert abs(p.sleep - 0.08) < 1e-9
    p.on_failure()                  # the shorter gap failed -> fall back and lock it in
    assert abs(p.sleep - 0.1) < 1e-9
    assert p.minimum_sleep_found is True
    assert abs(p.observed_min_sleep - 0.1) < 1e-9


def test_repeated_failures_pin_the_floor_to_the_current_sleep():
    p = Pacing(max_failures=3, exponential_backoff_threshold=0.5)
    p.set_sleep_bounds(0.1, 3.0)
    p.on_failure()                  # 0.1 -> 0.2
    p.on_failure()                  # 0.2 -> 0.4
    p.on_failure()                  # 0.4 -> 0.8, then the 3rd failure pins the floor
    assert abs(p.sleep - 0.8) < 1e-9
    assert abs(p.observed_min_sleep - 0.8) < 1e-9
    assert p.failure_count == 0     # the failure counter resets after pinning


def test_next_sleep_is_never_negative():
    p = Pacing()
    p.set_sleep_bounds(0.1, 3.0)
    p.sleep = -5                    # a pathological value never survives to a real sleep()
    assert p.next_sleep() == 0


def test_set_sleep_bounds_resets_the_controller_to_a_fresh_hunt():
    p = Pacing()
    p.set_sleep_bounds(0.1, 3.0)
    p.on_failure()
    p.on_failure()                  # move the state well off its start
    assert p.sleep != 0.1
    p.set_sleep_bounds(0.05, 2.0)   # e.g. an RF-config change
    assert p.min_sleep == 0.05 and p.max_sleep == 2.0
    assert p.sleep == 0.05          # back to the new min
    assert p.failure_count == 0
    assert p.observed_min_sleep == float('inf')

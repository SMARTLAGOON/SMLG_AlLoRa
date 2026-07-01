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

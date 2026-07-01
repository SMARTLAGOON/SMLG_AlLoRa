"""Pacing — one home for the timing the protocol adapts as it runs.

A pure policy object: no radio, no I/O. It is fed the Time-on-Air-derived window bounds by
whoever owns the RF config (the `Connector`), so the adaptation is unit-testable off-device
— which it never was while it lived scattered on the `Connector` (and, for the sleep
controller, on the `Requester`).

Today it owns the **adaptive receive window** (v2's `adaptive_timeout`): it starts wide
(`max`) and tightens toward the observed round-trip on each good reply (EWMA), then
jitter-grows back toward `max` on a timeout — floored by the ToA `min` and the best
round-trip yet seen, capped at `max`. The inter-request sleep controller folds in here
next (the second half of the "one home").
"""
from os import urandom

_SMOOTHING = 0.2   # EWMA weight of the newest round-trip when tightening the window


class Pacing:

    def __init__(self, min_timeout=0.5, max_timeout=6.0):
        self.observed_min_timeout = float('inf')
        self.set_bounds(min_timeout, max_timeout)

    def set_bounds(self, min_timeout, max_timeout):
        """Set the ToA-derived window bounds and reset the window to `max`. Called at config
        time and on every RF-config change."""
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout
        self.window = max_timeout

    def on_timeout(self):
        """No reply arrived within the window — jitter-grow it back toward `max`."""
        random_factor = int.from_bytes(urandom(2), "little") / 2**16
        self.window = min(self.window * (1 + random_factor), self.max_timeout)

    def on_reply(self, td):
        """A reply arrived in `td` seconds — tighten the window toward it (EWMA), floored by
        the ToA `min` and the best round-trip yet seen."""
        new_window = self.window * (1 - _SMOOTHING) + td * _SMOOTHING
        self.observed_min_timeout = min(self.observed_min_timeout, td)
        self.window = max(new_window, max(self.min_timeout, self.observed_min_timeout))

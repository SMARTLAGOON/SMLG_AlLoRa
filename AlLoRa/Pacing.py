"""Pacing: one home for the timing the protocol adapts as it runs.

A pure policy object: no radio, no I/O. It is fed the Time-on-Air-derived bounds by whoever
owns the RF config (the `Connector` for the window; the collector for the sleep), so the
adaptation is unit-testable off-device, which it never was while it lived scattered on the
`Connector` and on the collector.

Two adaptive controllers live here, the two halves of the "one home":

  * The **adaptive receive window** (v2's `adaptive_timeout`): it starts wide (`max`) and
    tightens toward the observed round-trip on each good reply (EWMA), then jitter-grows back
    toward `max` on a timeout, floored by the ToA `min` and the best round-trip yet seen,
    capped at `max`. Driven by `set_bounds` / `on_timeout` / `on_reply`.

  * The **inter-request sleep controller** (v2's `NEXT_ACTION_TIME_SLEEP`): the gap the
    initiator waits between rounds. It starts at the sf/bw-derived `min` and hunts for the
    shortest gap the link tolerates, probing shorter after a run of successes, backing off
    (exponential below a threshold, jittered above it) on failure, and pinning a floor once
    it has found the edge. Driven by `set_sleep_bounds` / `on_success` / `on_failure`, read
    via `next_sleep`.

A given deployment drives whichever half it needs (the `Connector` the window, the
collector the sleep); a step-4 `request`/`respond` engine will drive both from one home.
"""
from os import urandom

_SMOOTHING = 0.2   # EWMA weight of the newest round-trip when tightening the window


class Pacing:

    def __init__(self, min_timeout=0.5, max_timeout=6.0,
                 successful_interactions_required=5, max_failures=3,
                 exponential_backoff_threshold=0.5):
        # --- adaptive receive window ---
        self.observed_min_timeout = float('inf')
        self.set_bounds(min_timeout, max_timeout)
        # --- inter-request sleep controller (its bounds arrive later via set_sleep_bounds,
        # which needs the sf/bw the collector reads off its connector) ---
        self.successful_interactions_required = successful_interactions_required
        self.max_failures = max_failures
        self.exponential_backoff_threshold = exponential_backoff_threshold
        self.set_sleep_bounds(0.1, 3.0)

    def set_bounds(self, min_timeout, max_timeout):
        """Set the ToA-derived window bounds and reset the window to `max`. Called at config
        time and on every RF-config change."""
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout
        self.window = max_timeout

    def on_timeout(self):
        """No reply arrived within the window. Jitter-grow it back toward `max`."""
        random_factor = int.from_bytes(urandom(2), "little") / 2**16
        self.window = min(self.window * (1 + random_factor), self.max_timeout)

    def on_reply(self, td):
        """A reply arrived in `td` seconds. Tighten the window toward it (EWMA), floored by
        the ToA `min` and the best round-trip yet seen."""
        new_window = self.window * (1 - _SMOOTHING) + td * _SMOOTHING
        self.observed_min_timeout = min(self.observed_min_timeout, td)
        self.window = max(new_window, max(self.min_timeout, self.observed_min_timeout))

    # --- inter-request sleep controller (was scattered across the collector's loop) --------

    def set_sleep_bounds(self, min_sleep, max_sleep):
        """Set the sf/bw-derived sleep bounds and reset the controller to a fresh hunt
        (sleep = `min`). Called at config time and on every RF-config change; the constants
        (`successful_interactions_required`, `max_failures`, threshold) persist across a
        reset, matching the original `reset_sleep_time`."""
        self.min_sleep = min_sleep
        self.max_sleep = max_sleep
        self.sleep = min_sleep
        self.observed_min_sleep = float('inf')
        self.observed_max_sleep = 0
        self.sleep_delta = 0.1
        self.successful_interactions_count = 0
        self.minimum_sleep_found = False
        self.sleep_just_decreased = False
        self.last_sleep_time = self.sleep
        self.failure_count = 0

    def next_sleep(self):
        """The inter-request sleep to apply before the next round (never negative)."""
        return max(0, self.sleep)

    def on_success(self):
        """A round completed. Count it, and after a run of successes probe a shorter sleep,
        remembering the pre-probe value to fall back to if the probe fails."""
        self.successful_interactions_count += 1
        if self.successful_interactions_count >= self.successful_interactions_required:
            self.last_sleep_time = self.sleep
            self._decrease_sleep()
            self.sleep_just_decreased = True
            self.failure_count = 0

    def on_failure(self):
        """A round failed. Back the sleep off. If the failure immediately followed a probe
        decrease, treat the pre-probe value as the newly-found floor; after enough consecutive
        failures, pin the floor to the current sleep so we stop probing below it."""
        self._increase_sleep()
        self.successful_interactions_count = 0
        self.failure_count += 1
        if self.sleep_just_decreased:
            self.sleep_just_decreased = False
            self.minimum_sleep_found = True
            self.sleep = self.last_sleep_time
            self.observed_min_sleep = self.last_sleep_time
        if self.failure_count >= self.max_failures:
            self.observed_min_sleep = self.sleep
            self.sleep = self.observed_min_sleep
            self.failure_count = 0

    def _increase_sleep(self):
        """Back off: double while below the threshold (fast recovery from a too-short gap),
        jitter-grow toward `max` above it."""
        if self.sleep < self.exponential_backoff_threshold:
            self.sleep *= 2
        else:
            random_factor = int.from_bytes(urandom(2), "little") / 2**16
            self.sleep = min(self.sleep * (1 + random_factor), self.max_sleep)

    def _decrease_sleep(self):
        """Probe shorter: EWMA-shrink toward zero, floored by an absolute minimum and the
        best gap the link has tolerated so far."""
        smoothing_factor = 0.2
        absolute_min_sleep = 0.01
        new_sleep_time = self.sleep * (1 - smoothing_factor)
        new_sleep_time = max(absolute_min_sleep, new_sleep_time)
        if self.minimum_sleep_found and new_sleep_time < self.observed_min_sleep:
            self.observed_min_sleep = new_sleep_time
        elif not self.minimum_sleep_found:
            self.observed_min_sleep = new_sleep_time
        self.sleep = max(new_sleep_time, self.observed_min_sleep)

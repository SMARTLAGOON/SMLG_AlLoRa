"""Wrap-safe tick arithmetic — one semantic on both runtimes.

MicroPython's `utime.ticks_ms()` wraps at 2^30 ms (~12.4 days). Any deadline built
with raw `+`/`<` on those ticks misbehaves when the window spans the wrap, so all
deadline math in the protocol goes through `ticks_add` / `ticks_diff`: identical in
meaning to the `utime` pair, implemented portably for CPython.
"""
from AlLoRa.utils.time_utils import ticks_add, ticks_diff

PERIOD = 1 << 30    # MicroPython's tick period


def test_ticks_roundtrip_far_from_the_wrap():
    t = 123_456_789
    assert ticks_diff(ticks_add(t, 500), t) == 500
    assert ticks_diff(t, ticks_add(t, 500)) == -500


def test_ticks_add_wraps_at_the_period():
    assert ticks_add(PERIOD - 100, 250) == 150
    assert ticks_add(50, -100) == PERIOD - 50


def test_ticks_diff_spans_the_wrap():
    before = PERIOD - 100
    after = 150                       # 250 ms later, across the wrap
    assert ticks_diff(after, before) == 250
    assert ticks_diff(before, after) == -250


def test_ticks_diff_accepts_unwrapped_epoch_ms():
    # On CPython current_time_ms() is epoch ms (far beyond the period); congruence
    # keeps the diffs exact as long as the true gap is under half a period.
    epoch = 1_784_459_143_000
    assert ticks_diff(epoch + 5000, epoch) == 5000
    assert ticks_diff(ticks_add(epoch, 3000), epoch) == 3000


def test_deadline_pattern_survives_the_wrap():
    # The pattern every window uses: deadline = ticks_add(now, w); expired when
    # ticks_diff(deadline, now) <= 0. Starting 1 s before the wrap, a 2 s window
    # must still be open after 1.5 s and closed after 2.5 s.
    start = PERIOD - 1000
    deadline = ticks_add(start, 2000)
    assert ticks_diff(deadline, ticks_add(start, 1500)) > 0    # still open
    assert ticks_diff(deadline, ticks_add(start, 2500)) <= 0   # expired

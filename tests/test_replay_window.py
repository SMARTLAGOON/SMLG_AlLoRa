"""Unit — the sliding anti-replay window (RFC 6479-style).

Secure mode authenticates a 2-byte frame counter in every frame's AAD. The receiver
must accept each counter **once** and reject a re-injected (identical, therefore same
counter) frame. Because v3's windowed/selective-repeat transfer will deliver frames
slightly out of order, the check is a *sliding window*, not strict-monotonic: a
fresh counter is accepted even if it arrives after a higher one, but an already-seen
counter — or one older than the window — is rejected.

This is receiver-local state only (no wire cost). The counter is per-*frame*: a
legitimately retransmitted chunk rides a new frame with a new counter, so it is never a
replay; only an attacker replaying captured bytes reuses a counter.
"""
from AlLoRa.Security.Replay_window import Replay_window


def test_first_counter_is_accepted():
    w = Replay_window()
    assert w.accept(1) is True


def test_exact_replay_of_an_accepted_counter_is_rejected():
    w = Replay_window()
    assert w.accept(5) is True
    assert w.accept(5) is False  # same frame re-injected -> replay


def test_out_of_order_but_fresh_counter_is_accepted():
    # A gap opens (10 then 12), then the missing frame arrives late. It is still fresh,
    # so the window fills the gap and accepts it — but only once.
    w = Replay_window()
    assert w.accept(10) is True
    assert w.accept(12) is True   # 11 is now a hole in the window
    assert w.accept(11) is True   # late but never seen -> accept
    assert w.accept(11) is False  # now it's a replay
    assert w.accept(10) is False  # earlier accept still remembered


def test_counter_older_than_the_window_is_rejected_as_stale():
    # Once the highest advances far past an old counter, the window can no longer prove
    # that counter wasn't already seen, so it is rejected outright.
    w = Replay_window()
    assert w.accept(1) is True
    assert w.accept(1 + Replay_window.WINDOW) is True   # advances the window past 1
    assert w.accept(1) is False                         # 1 fell out of the window -> stale

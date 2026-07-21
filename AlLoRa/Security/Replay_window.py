"""A sliding anti-replay window over an integer frame counter (RFC 6479 style).

The receiver of a secure session calls ``accept(counter)`` once per authenticated frame.
It answers a single question: *have I already seen this counter, or is it too old to
judge?* A fresh counter is accepted (and remembered); a counter already seen, or one
that has fallen out of the window, is rejected.

The window tolerates bounded reordering (a fresh counter arriving after a higher one is
still accepted while it stays within ``WINDOW`` of the highest), which v3's
selective-repeat transfer needs; strict-monotonic checking would drop legitimately
reordered frames. State is a single highest-seen value plus a ``WINDOW``-bit mask, a few
bytes per session, nothing on the wire.

Counters are expected to be positive (the send side starts at 1); ``0`` is the
"nothing accepted yet" sentinel.
"""


class Replay_window:

    WINDOW = 64                      # frames of reordering tolerance
    _MASK = (1 << WINDOW) - 1

    def __init__(self):
        self._highest = 0            # highest counter accepted so far (0 = none yet)
        self._bitmap = 0             # bit k set == counter (highest - k) has been seen

    def accept(self, counter):
        """Return True if ``counter`` is fresh (and record it); False if replay/stale."""
        if counter > self._highest:
            shift = counter - self._highest
            if shift >= self.WINDOW:
                self._bitmap = 1                                       # every old bit fell out
            else:
                self._bitmap = ((self._bitmap << shift) & self._MASK) | 1  # bit0 = new highest
            self._highest = counter
            return True

        offset = self._highest - counter
        if offset >= self.WINDOW:
            return False             # older than the window -> can't prove it's not a replay
        bit = 1 << offset
        if self._bitmap & bit:
            return False             # already seen -> replay
        self._bitmap |= bit
        return True

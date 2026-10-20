"""Rejects replayed frames, using a sliding window over the frame counter (as in RFC 6479).

Call `accept(counter)` once per authenticated frame. It rejects repeats and counters more than
`WINDOW` below the highest, but allows reordering inside the window, which selective repeat
needs. Counters start at 1.
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

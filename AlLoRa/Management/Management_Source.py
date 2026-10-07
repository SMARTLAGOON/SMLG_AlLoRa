"""Where a Hub receives changes to its node list: which nodes are active, timings, names.

It never carries commands, and only a Hub has one: on an Edge it would bypass signed control.
Subclasses override `check()` to fetch changes and `report()` to say what is running. Only the
newest change is kept, and `take_intent` removes it.
"""


class Management_Source:

    def __init__(self):
        # The newest wish, or None. One value and not a list: see the module docstring.
        self._intent = None
        # The last thing the Hub said it was running, kept so whichever half performs the
        # exchange already holds it. The Hub pushes this in rather than the source reaching
        # back into the node, which is the coupling every other boundary here avoids.
        self._report = None

    # --- lifecycle ---------------------------------------------------------------------

    def prepare(self):
        """Acquire whatever this boundary needs (a client, a socket). The base acquires
        nothing. Called at registration so a connect that must fail fails loudly at setup."""
        pass

    def close(self):
        """Release whatever prepare() acquired. Idempotent; the base holds nothing."""
        pass

    # --- the up direction ---------------------------------------------------------------

    def report(self, report):
        """Take what the Hub is currently running.

        Called from the visit loop, so nothing here may block, allocate much, or raise: the
        loop's next act is to listen to an endpoint. A transport subclass keeps the value and
        sends it from its own worker, or attaches it to the request `check()` makes.
        """
        self._report = report

    def last_report(self):
        return self._report

    # --- the down direction --------------------------------------------------------------

    def check(self):
        """Non-blocking pump, called by the visit loop between visits.

        The base has nothing to ask, so this does nothing and the slot is filled from outside
        instead. A subclass that goes and asks does it here, and on a host that means from a
        worker thread rather than in this call: an exchange performed inline would put a site's
        latency, and a stalled site's whole timeout, between two radio exchanges.
        """
        pass

    def submit_intent(self, intent):
        """Fill the slot with the newest desired state. Latest wins, and nothing expires: a
        Hub that comes back after a week picks up what was decided a month ago, which is the
        case this shape exists to serve."""
        self._intent = intent

    def has_intent(self):
        return self._intent is not None

    def take_intent(self):
        """The newest wish, and empty the slot. None when there is nothing new to say."""
        intent = self._intent
        self._intent = None
        return intent

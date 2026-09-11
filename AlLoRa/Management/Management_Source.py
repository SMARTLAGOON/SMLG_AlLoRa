"""Management_Source, a Hub's management boundary: where it learns what it should be running.

A sibling of `DataSource`, `DataSink` and the status `Subscriber`, and named the same way they
are, prefix plus plug type: the prefix says what flows, the plug type says which direction.
`DataSource` never meant "the source role", it meant *source of Data*, and this decomposes
identically, so it is a sibling by construction rather than by analogy.

**It is not a control boundary, and the distinction is the whole design.** A *command* changes
what a node is: it crosses the LoRa link, carries a control type, is signed by the control root
and is checked by the verify gate. A *roster change* changes what a **Hub** does: `active`, the
four timing values and a node's name are read by the Hub alone, out of the `Nodes.json` entry
behind each `Digital_Endpoint`, and an Edge never learns any of them. Nothing here reaches the
air, so none of the trust model applies to it, and a design that treated the two as one concept
would inherit a downlink requirement it does not have.

**A Hub holds one of these and an Edge never does.** A node holding a control root refuses
unsigned in-band control from then on, so that the signature is protecting something; an Edge
holding this boundary would be a second, unsigned way to change that node's settings. The Hub is
different because it is the trust anchor and the authenticated peer, and its own endpoint
entries are its own business. That is a rule about trust, and a class name cannot carry a
reason, so it is written here and enforced by where the verbs live: on `Hub`, on nothing else.

**Nothing is delivered, so nothing needs a delivery guarantee.** This is not the sink's
contract next door, where an artifact must arrive exactly once. It carries desired state: the
newest wish replaces the last one, there is no queue and no acknowledgement, and a change that
gets lost keeps not matching what the Hub reports and simply goes out again. So `take_intent`
is a plain take rather than the peek-retain a `DataSource` performs, and the difference is
deliberate.

**One slot, two fillers.** On an SBC an API thread calls `submit_intent` from outside the loop.
On a microcontroller acting alone as a Hub there is no thread to spare, so the loop pumps
`check()` between visits and the subclass fills the slot from there. The slot and the drain are
identical on both, which is why they are in the library rather than in a deployment.

The base class is a working boundary in its own right: a slot, and the verbs to fill and empty
it. A transport subclass overrides `check()` to go and ask, and `report()` to say what this Hub
is running in the same exchange.
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

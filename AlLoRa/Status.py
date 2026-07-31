"""Status: what a running device reports about itself, and who is watching it.

A small holder for the live values (RF config, the file and chunk in flight, packet sizes,
timings, error counts) plus the subscribers those values are pushed to. It knows nothing
about packets, sessions or radios, which is the point of it being its own thing: a bridge
board that runs no protocol at all still wants to drive a screen, and it should not have to
inherit a node to do it.

Two shapes are load-bearing here because the code around it already depends on them:

  * Values are set and read dict-style (``status['SF'] = 12``), which is how the node loops
    write them in the middle of a transfer.
  * What crosses to a subscriber is the values dict itself, not this object. Subscribers do
    ``status.get(...)``, ``key in status`` and json.dumps over it, and a screen keeps the
    reference it was handed and re-reads it on every refresh, so the dict is updated in
    place and never replaced.

A subscriber is anything with ``update(status)``.
"""


class Status:

    def __init__(self, values=None):
        self.values = values if values is not None else {}
        self.subscribers = []

    # --- the values ---------------------------------------------------------------------

    def __setitem__(self, key, value):
        self.values[key] = value

    def __getitem__(self, key):
        return self.values[key]

    # --- who is watching ----------------------------------------------------------------

    def register(self, subscriber):
        if subscriber not in self.subscribers:
            self.subscribers.append(subscriber)

    def unregister(self, subscriber):
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)

    def notify(self):
        for subscriber in self.subscribers:
            subscriber.update(self.values)

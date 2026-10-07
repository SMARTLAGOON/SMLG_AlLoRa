"""A device's live values (radio settings, transfer progress, timings) and their subscribers.

Set values like a dict: `status['SF'] = 12`. Subscribers get the dict itself, and a screen keeps
that reference, so the dict is updated in place and never replaced. A subscriber is any object
with `update(status)`.
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

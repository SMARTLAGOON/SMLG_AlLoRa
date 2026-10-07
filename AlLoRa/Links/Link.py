"""A byte pipe between the two halves of a split Connector. It never looks inside the frames.

The logic side calls `rpc()` and `read_reply()`; the radio side calls `read_request()` and
`write_reply()`. `read_reply` picks up a late reply on a serial line. Over HTTP each reply comes
back with its request, so there it returns None.
"""


class Link:

    def rpc(self, request, timeout=None):
        """Client half: send a request frame down, block up to `timeout` for the reply frame.
        Returns the reply bytes (or None on timeout)."""
        raise NotImplementedError

    def read_reply(self, timeout=None):
        """Client half: block up to `timeout` for the next reply frame, sending nothing.

        The default is None, which is the honest answer for a medium that cannot hand back a
        frame the client did not just ask for. Overridden by the links that stream.
        """
        return None

    def read_request(self, timeout=None):
        """Bridge half: block up to `timeout` for the next request frame. None on timeout."""
        raise NotImplementedError

    def write_reply(self, reply):
        """Bridge half: send a reply frame back up to the waiting client."""
        raise NotImplementedError

    def close(self):
        pass

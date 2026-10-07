"""A Link inside one process, for testing the tunnel on a computer without a cable or socket.

It uses CPython's `queue`, so it is never put into board firmware.
"""
import queue

from AlLoRa.Links.Link import Link


class Loopback_link(Link):

    def __init__(self, down, up):
        # down: requests (client -> bridge); up: replies (bridge -> client).
        self._down = down
        self._up = up

    @staticmethod
    def create_pair():
        """Return (client_half, bridge_half) wired back-to-back over the same two queues."""
        down = queue.Queue()
        up = queue.Queue()
        return Loopback_link(down, up), Loopback_link(down, up)

    def rpc(self, request, timeout=None):
        self._down.put(request)
        try:
            return self._up.get(timeout=timeout)
        except queue.Empty:
            return None

    def read_reply(self, timeout=None):
        try:
            return self._up.get(timeout=timeout)
        except queue.Empty:
            return None

    def read_request(self, timeout=None):
        try:
            return self._down.get(timeout=timeout)
        except queue.Empty:
            return None

    def write_reply(self, reply):
        self._up.put(reply)

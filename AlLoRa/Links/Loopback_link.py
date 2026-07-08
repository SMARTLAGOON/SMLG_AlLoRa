"""Loopback_link — an in-process Link for testing the tunnel on CPython, with no UART/socket.

Whatever the client half `rpc`s down, the bridge half `read_request`s; whatever the bridge
`write_reply`s, the client's blocked `rpc` returns. Two directions, two queues, so a pending
reply is never mistaken for the next request. CPython-only (uses `queue`) — it never freezes
to firmware, exactly like Loopback_connector.
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

    def read_request(self, timeout=None):
        try:
            return self._down.get(timeout=timeout)
        except queue.Empty:
            return None

    def write_reply(self, reply):
        self._up.put(reply)

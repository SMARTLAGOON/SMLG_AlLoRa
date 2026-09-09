"""Link: the byte pipe between a split Connector's two halves.

A tunnel splits a Connector across a slow link: the logic-holder runs the protocol engine +
codec + keys and forwards the transport verbs; the bridge (Adapter) holds the radio and runs
them. The Link is the transport underneath that forwarding: it moves opaque request/reply
frames and knows *nothing* about verbs, the LoRa air wire, or session keys. That ignorance is
the point: adding a BLE or USB tunnel is a new Link, never new protocol logic.

Two halves of the same link object graph:

  * client half (logic-holder):  rpc(request) -> reply      send a request, block for its reply
                                 read_reply() -> reply      take the next one without asking
  * bridge half (Adapter):       read_request() -> request  ;  write_reply(reply)

`read_reply` exists because on a streaming medium a reply can outlive the question it answers:
the bridge blocks at its radio for the length of a window, so a reply the client has stopped
waiting for is still on its way. The client half discards such a frame by its call id and needs
somewhere to take the next one from. A medium that pairs a response to its request by
construction (an HTTP body) has no second frame to offer and says so by returning None.

Concrete links (Serial_link, WiFi_link) delimit frames on their medium (a UART sentinel, an
HTTP body); Loopback_link backs the contract with in-process queues so the tunnel is testable
on CPython.
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

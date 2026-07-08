"""Link — the byte pipe between a split Connector's two halves.

A tunnel splits a Connector across a slow link: the logic-holder runs the protocol engine +
codec + keys and forwards the transport verbs; the bridge (Adapter) holds the radio and runs
them. The Link is the transport underneath that forwarding — it moves opaque request/reply
frames and knows *nothing* about verbs, the LoRa air wire, or session keys. That ignorance is
the point: adding a BLE or USB tunnel is a new Link, never new protocol logic.

Two halves of the same link object graph:

  * client half (logic-holder):  rpc(request) -> reply      send a request, block for its reply
  * bridge half (Adapter):       read_request() -> request  ;  write_reply(reply)

Concrete links (Serial_link, WiFi_link) delimit frames on their medium (a UART sentinel, an
HTTP body); Loopback_link backs the contract with in-process queues so the tunnel is testable
on CPython.
"""


class Link:

    def rpc(self, request, timeout=None):
        """Client half: send a request frame down, block up to `timeout` for the reply frame.
        Returns the reply bytes (or None on timeout)."""
        raise NotImplementedError

    def read_request(self, timeout=None):
        """Bridge half: block up to `timeout` for the next request frame. None on timeout."""
        raise NotImplementedError

    def write_reply(self, reply):
        """Bridge half: send a reply frame back up to the waiting client."""
        raise NotImplementedError

    def close(self):
        pass

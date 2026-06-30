import queue

from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print


class Loopback_connector(Connector):
    """An in-memory `Connector` for testing the protocol on CPython, with no radio.

    The `Connector` is the v3 transport seam (ADR 0001 §2): the protocol engine
    (`Source` / `Collector`) talks only to `send` / `recv` / `send_and_wait_response`,
    never to a chip. This implementation backs that contract with a pair of in-process
    queues instead of a radio, so a `Source` and a `Collector` can run a full file
    transfer in a test — the proof that the seam is clean.

    Build a connected pair with `Loopback_connector.create_pair(mac_a, mac_b)`:
    whatever one end `send`s, the other end `recv`s. The link is lossless and
    in-order; loss/reorder injection (for the retransmission and role-swap gates)
    is a later, deliberate extension.
    """

    def __init__(self, mac, inbox=None, outbox=None):
        super().__init__()
        self.MAC = mac
        # inbox: bytes this connector receives; outbox: bytes it transmits to the peer.
        self.inbox = inbox if inbox is not None else queue.Queue()
        self.outbox = outbox if outbox is not None else queue.Queue()

    @staticmethod
    def create_pair(mac_a, mac_b):
        """Return two connectors wired back-to-back: a's outbox is b's inbox."""
        a_to_b = queue.Queue()
        b_to_a = queue.Queue()
        a = Loopback_connector(mac_a, inbox=b_to_a, outbox=a_to_b)
        b = Loopback_connector(mac_b, inbox=a_to_b, outbox=b_to_a)
        return a, b

    def get_mac(self):
        return self.MAC

    def send(self, packet):
        # The wire carries the framed bytes, exactly as a radio would put on air.
        self.outbox.put(packet.get_content())
        return True

    def recv(self, focus_time=12):
        # Block up to focus_time seconds for a frame, mirroring a radio's receive window.
        try:
            return self.inbox.get(timeout=focus_time)
        except queue.Empty:
            return None

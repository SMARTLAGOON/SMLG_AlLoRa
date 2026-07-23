import queue
import random

from AlLoRa.Connectors.Connector import Connector
from AlLoRa.utils.debug_utils import print


class Loopback_connector(Connector):
    """An in-memory `Connector` for testing the protocol on CPython, with no radio.

    The `Connector` is the v3 transport seam: the protocol engine (`Source` / `Collector`)
    talks only to `send` / `recv` / `send_and_wait_response`, never to a chip. This
    implementation backs that contract with a pair of in-process queues instead of a radio,
    so a `Source` and a `Collector` can run a full file transfer in a test, the proof that
    the seam is clean.

    Build a connected pair with `Loopback_connector.create_pair(mac_a, mac_b)`:
    whatever one end `send`s, the other end `recv`s. The link is in-order; pass a
    per-direction `loss` probability + a `seed` to drop frames deterministically and
    exercise the retransmission / coordinated-transition gates (lost-packet injection).

    CPython-only (uses `queue` / `random` / threads). It never freezes to firmware.
    """

    def __init__(self, mac, inbox=None, outbox=None, loss=0.0, rng=None):
        super().__init__()
        self.MAC = mac
        # inbox: bytes this connector receives; outbox: bytes it transmits to the peer.
        self.inbox = inbox if inbox is not None else queue.Queue()
        self.outbox = outbox if outbox is not None else queue.Queue()
        self.loss = loss          # probability a transmitted frame is lost in the channel
        self._rng = rng           # a random.Random driving the loss decisions, or None
        self.dropped = 0          # frames this end transmitted but the channel dropped

    @staticmethod
    def create_pair(mac_a, mac_b, loss_a_to_b=0.0, loss_b_to_a=0.0, seed=None):
        """Return two connectors wired back-to-back: a's outbox is b's inbox.

        `loss_a_to_b` / `loss_b_to_a` drop that fraction of frames in that direction.
        Each direction gets its own `random.Random(seed)` so its drop sequence is
        deterministic as long as one thread drives that end's `send` (the usual case:
        one node per connector).
        """
        a_to_b = queue.Queue()
        b_to_a = queue.Queue()
        rng_a = random.Random(seed) if (seed is not None and loss_a_to_b) else None
        rng_b = random.Random(seed) if (seed is not None and loss_b_to_a) else None
        a = Loopback_connector(mac_a, inbox=b_to_a, outbox=a_to_b, loss=loss_a_to_b, rng=rng_a)
        b = Loopback_connector(mac_b, inbox=a_to_b, outbox=b_to_a, loss=loss_b_to_a, rng=rng_b)
        return a, b

    def get_mac(self):
        return self.MAC

    def transmit(self, wire):
        # The wire carries the already-framed bytes, exactly as a radio would put on air.
        if self.loss and self._rng is not None and self._rng.random() < self.loss:
            # Lost in the channel: the transmitter still "sent" fine (returns True),
            # the receiver simply never sees it. Its recv will time out and the
            # protocol retransmits.
            self.dropped += 1
            return True
        self.outbox.put(wire)
        return True

    def recv(self, focus_time=12):
        # Block up to focus_time seconds for a frame, mirroring a radio's receive window.
        try:
            return self.inbox.get(timeout=focus_time)
        except queue.Empty:
            return None

    # RF setters: the base Connector stubs these for a real radio to override (configure the
    # chip, then update the field). With no chip, the loopback just updates the field, so
    # change_rf_config actually moves this connector's (freq, sf, bw, cr, tx_power) — the
    # RF-config coordination tests need a loopback that models a real reconfiguration.
    def set_frequency(self, frequency):
        self.frequency = frequency

    def set_sf(self, sf):
        self.sf = sf

    def set_bw(self, bw):
        self.bw = bw

    def set_cr(self, cr):
        self.cr = cr

    def set_transmission_power(self, tx_power):
        self.tx_power = tx_power

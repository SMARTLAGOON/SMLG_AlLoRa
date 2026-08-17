"""A frame the radio says is damaged is dropped at the radio, and still counted as corrupt.

The SX1276 computes a CRC over the payload and raises PayloadCrcError when it fails. The
receive path never read that flag: `pyLora.recv()` returned the FIFO contents and
`SX127x_connector.recv()` passed them straight up, so a damaged frame reached AlLoRa framing.
Framing rejected most of them, which is why the symptom looked like endless retransmission
rather than corruption, and it is why this went unnoticed until a raw sweep at SF12 checked
delivered bytes against sent bytes and found 17 of 18 frames damaged, every one of them with
the correct declared length. That combination is diagnostic: the explicit header carries its
own CRC at a stronger coding rate, so the length field survives while the payload disintegrates.

Two halves here, because dropping the frame silently would trade one blind spot for another:

**At the radio** (the `pyLora` half) the flag is read and the frame is thrown away, so
corruption never reaches framing at all. The SX1262 connector already behaves this way,
returning None when its driver reports a bad state, so this brings the SX127x path in line
with the driver beside it rather than inventing a convention.

**In the accounting** (the `Connector` half) the drop is reported as a corrupt frame rather
than as an empty window. `Node` counts `status['CorruptedPackets']` from that label, and it is
the number the bench arms are compared on: an arm logged 22 corrupt packets against another's
1. Dropping the frame at the radio without saying so would have moved those 22 into the
retransmission count and made every corrupt-packet number recorded before the fix
incomparable with every number after it.
"""
import queue

import pytest

import fake_sx127x
from fake_sx127x import PAYLOAD_CRC_ERROR, RX_DONE, build_pylora

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Packet_v3 import Packet_v3
from PyLora_SX127x_extensions.board_config import BOARD

FRAME = bytes([0x2A, 0x32, 0x02, 0xAB, 0xCD, 0xEF]) + b"the body of an ordinary chunk"


def _radio():
    radio, spi = build_pylora(sf=9, bw=125)
    radio.settimeout(1)         # the polling receive path, which is what a node uses
    radio.setblocking(False)
    return radio, spi


# --- at the radio -----------------------------------------------------------------------

def test_construction_turns_the_payload_crc_on():
    # The premise of everything below: the modem is asked for the CRC at construction
    # (rx_crc=True), so the flag the receive path reads is really being computed. If this
    # write ever stopped landing, the drop below would be dead code and nothing else would say so.
    _, spi = _radio()

    assert spi.payload_crc_is_on()


def test_an_intact_frame_is_handed_up():
    radio, spi = _radio()
    spi.arrive(FRAME)

    assert radio.recv(255) == FRAME


def test_a_frame_that_failed_the_payload_crc_is_not_handed_up():
    radio, spi = _radio()
    spi.arrive(FRAME, crc_error=True)

    assert radio.recv(255) is None


def test_the_crc_flag_survives_the_rxdone_clear():
    # What the fix rests on. The IRQ register is write-one-to-clear and the receive callback
    # clears RxDone alone, so PayloadCrcError is still standing to be read afterwards. Clear
    # both together and the fix silently stops working, with every test above still passing
    # for the wrong reason.
    _, spi = _radio()
    spi.arrive(FRAME, crc_error=True)

    fake_sx127x.FakeBoard.last.cb_dio0(None)    # the DIO0 rise, as the board delivers it

    assert not spi.flags() & RX_DONE, "the receive callback should have taken RxDone down"
    assert spi.flags() & PAYLOAD_CRC_ERROR, "and left the payload-CRC flag up to be read"


def test_the_crc_flag_is_cleared_so_the_next_frame_is_judged_on_its_own():
    # A latched flag would condemn every later frame on this radio.
    radio, spi = _radio()
    spi.arrive(FRAME, crc_error=True)
    assert radio.recv(255) is None

    spi.arrive(FRAME)

    assert not spi.flags() & PAYLOAD_CRC_ERROR
    assert radio.recv(255) == FRAME


def test_an_empty_window_still_raises_the_timeout_the_connector_catches():
    # The drop must not be confused with silence: nothing arriving still raises, which is what
    # SX127x_connector.recv turns into its own None.
    radio, _ = _radio()

    with pytest.raises(BOARD.LoRaTimeoutError):
        radio.recv(255)


# --- in the accounting ------------------------------------------------------------------

class _DroppingLoopback(Loopback_connector):
    """A loopback whose radio throws away the frame it received as damaged.

    It stands in for the SX127x connector, which cannot be imported on CPython (it reaches for
    `network` and `ubinascii`). What is under test is the base `Connector`'s accounting, and
    that is the same code on either connector.
    """

    def recv(self, focus_time=12):
        wire = super().recv(focus_time)
        if wire is None:
            return None                       # nothing arrived: an ordinary empty window
        self.recv_dropped_corrupt = True      # arrived damaged, and the radio dropped it
        return None


def _pair():
    a_to_b, b_to_a = queue.Queue(), queue.Queue()
    a = _DroppingLoopback("a1a1a1a1", inbox=b_to_a, outbox=a_to_b)
    b = Loopback_connector("b2b2b2b2", inbox=a_to_b, outbox=b_to_a)
    for connector in (a, b):
        connector.config({"name": "N", "protocol_version": 3, "addressing": "sid"})
    return a, b


def _packet(sid=7):
    p = Packet_v3(addressing="sid")
    p.set_session(sid)
    p.ask_metadata()
    return p


def test_a_frame_dropped_at_the_radio_is_reported_as_corrupt_not_as_silence():
    a, b = _pair()
    b.transmit(_packet().get_content())        # a reply is waiting for a, and will be dropped
    a.adaptive_timeout = 0.3

    error, _, _, _ = a.send_and_wait_response(_packet())

    assert error["type"] == "CORRUPTED_PACKET"


def test_a_frame_dropped_at_the_radio_leaves_the_receive_window_alone():
    # The frame arrived inside the window, so the window was wide enough and growing it would
    # slow every later round for a reason that is not true. This also keeps the pacing
    # behaviour identical to what it was before the drop moved down to the radio, where a
    # corrupt frame reached framing, failed to parse, and did not touch the window either.
    a, b = _pair()
    b.transmit(_packet().get_content())
    a.adaptive_timeout = 0.3

    a.send_and_wait_response(_packet())

    assert a.adaptive_timeout == 0.3


def test_an_empty_window_is_still_reported_as_a_timeout():
    a, _ = _pair()                             # nothing waiting for a
    a.adaptive_timeout = 0.3

    error, _, _, _ = a.send_and_wait_response(_packet())

    assert error["type"] == "TIMEOUT"


def test_the_corrupt_flag_does_not_leak_into_the_next_window():
    # One window's drop must not label the next window's silence. `listen` clears it before
    # every receive, so a connector that never sets it (every other connector today) keeps
    # reporting timeouts exactly as before.
    a, b = _pair()
    b.transmit(_packet().get_content())
    a.adaptive_timeout = 0.3
    assert a.send_and_wait_response(_packet())[0]["type"] == "CORRUPTED_PACKET"

    error, _, _, _ = a.send_and_wait_response(_packet())

    assert error["type"] == "TIMEOUT"

"""After a transmission the SX127x is listening again before the caller gets control back.

The driver used to switch the receiver on only when `recv()` was called. Between the end of a
transmission and that call the chip sat in standby, and anything sent to it was lost. On the
bench that gap was about 70 ms of the Edge's own software after every chunk, while the Hub
turned around and sent its next request in about the same time. Most rounds the Edge won by
about 20 ms; at a steady rhythm one round lost, the request went unheard, and the Hub waited
out a full receive window. The edge logs of the 2026-08-17 runs show the tightest rounds
switching on just as the request started, so the margin was already gone before any pacing.

The fix is the one the SX1262 connector already carries: put the receiver back on air at the
end of the transmission, and have `recv()` wait on a receiver that is already listening rather
than restart it. A restart passes through sleep, which aborts a frame that is mid-air.

`FakeSpi.on_air()` is what makes this testable: a frame lands only if the chip is listening at
that moment, which is the property the race turns on.
"""
import pytest

from fake_sx127x import (MODEM_CONFIG_1, MODEM_CONFIG_2, OP_MODE, PAYLOAD_CRC_ERROR, RX_DONE,
                         build_pylora)

from PyLora_SX127x_extensions.board_config import BOARD
from PyLora_SX127x_extensions.constants import MODE

REPLY = b"a chunk going out"
REQUEST = b"*2 the next request"


def _radio():
    radio, spi = build_pylora(sf=7, bw=125)
    radio.settimeout(1)
    radio.setblocking(False)
    return radio, spi


def _send(radio, wire):
    # The way SX127x_connector.transmit drives it: blocking with a timeout, then non-blocking.
    radio.settimeout(1)
    radio.setblocking(True)
    radio.send(wire)
    radio.setblocking(False)


def test_the_receiver_is_on_air_as_soon_as_send_returns():
    radio, spi = _radio()

    _send(radio, REPLY)

    assert spi.listening(), "the chip should be in receive, not standby, once send returns"


def test_a_request_sent_before_recv_is_called_is_not_lost():
    # The race itself: the peer answers while this node is still busy after its own send.
    radio, spi = _radio()
    _send(radio, REPLY)

    caught = spi.on_air(REQUEST)

    assert caught, "the request was transmitted while the chip was deaf"
    assert radio.recv(255) == REQUEST


def test_recv_on_a_listening_radio_does_not_restart_the_receiver():
    # A restart goes through sleep, which aborts a frame that is arriving at that moment.
    radio, spi = _radio()
    _send(radio, REPLY)
    spi.on_air(REQUEST)
    before = len(spi.writes_to(OP_MODE))

    radio.recv(255)

    sleeps = [w for w in spi.writes_to(OP_MODE)[before:] if w[1] == MODE.SLEEP]
    assert not sleeps


def test_a_frame_heard_before_the_send_is_not_taken_for_the_reply_to_it():
    # A node that listens all the time can hear something while it is busy, and then transmit.
    # Whatever is waiting from before that transmission must not be handed up after it as if
    # it were the answer: the wait after a send starts empty, as it did when recv restarted
    # the receiver every time.
    radio, spi = _radio()
    _send(radio, REPLY)
    spi.on_air(b"something from before")

    _send(radio, REPLY)

    assert not spi.flags() & (RX_DONE | PAYLOAD_CRC_ERROR)
    with pytest.raises(BOARD.LoRaTimeoutError):
        radio.recv(255)


def test_the_receiver_stays_on_air_after_a_frame_is_read():
    radio, spi = _radio()
    _send(radio, REPLY)
    spi.on_air(REQUEST)
    radio.recv(255)

    assert spi.on_air(b"the one after"), "the chip stopped listening after the first frame"
    assert radio.recv(255) == b"the one after"


def test_the_first_recv_still_switches_the_receiver_on():
    # A freshly configured radio has never transmitted, so nothing has armed it yet.
    radio, spi = _radio()
    assert not spi.listening()

    with pytest.raises(BOARD.LoRaTimeoutError):
        radio.recv(255)

    assert spi.listening()


@pytest.mark.parametrize("retune", [
    lambda radio: radio.set_spreading_factor(9),
    lambda radio: radio.sf(9),
    lambda radio: radio.set_coding_rate(2),
    lambda radio: radio.set_bandwidth(250),
    lambda radio: radio.set_frequency(868.3),
])
def test_a_retune_while_listening_lands_while_idle_and_keeps_listening(retune):
    # Modem configuration latches only in sleep or standby. A radio parked in receive has to
    # step out of it for the write and step back in afterwards, so the next frame is heard on
    # the new settings.
    radio, spi = _radio()
    _send(radio, REPLY)
    before = len(spi.modes_at_write)

    retune(radio)

    config_writes = [(reg, mode) for reg, mode in spi.modes_at_write[before:]
                     if reg in (MODEM_CONFIG_1, MODEM_CONFIG_2) or 0x06 <= reg <= 0x08]
    assert config_writes, "the retune wrote nothing the test can see"
    assert all(mode in (MODE.SLEEP, MODE.STDBY) for _, mode in config_writes), config_writes
    assert spi.listening()

"""The vendored SX126x driver, on a fake SPI bus.

The driver ships frozen into the firmware image and had no hardware-free rig at all, which is
how it carried a five millisecond crystal restart on the path back to receive for as long as
anyone had been using it. Three things were changed in it and all three are invisible from
above: the radio still comes back on air, just far too late for a peer that is already
replying. So the tests here watch the bus rather than the outcome.

The fake models the one property the changes turn on: the chip answers with a status byte for
every byte of a command's data phase, and a command costs what it costs to clock out. A fake
that only recorded method calls could not tell a rebuilt receive configuration from a resumed
one, which is the whole difference between a working Hub and a deaf one.
"""
import builtins
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "firmware", "drivers", "sx1262"))

# The driver writes `NAME = const(...)` without importing const, which is legal on the target
# because MicroPython's compiler folds those assignments and never looks the name up. CPython
# does look it up, so it gets the same answer here: const is the identity.
if not hasattr(builtins, "const"):
    builtins.const = lambda value: value

import _sx126x                                          # noqa: E402
import sx126x as driver                                 # noqa: E402
from _sx126x import (                                   # noqa: E402
    SX126X_CMD_SET_STANDBY, SX126X_CMD_SET_RX, SX126X_CMD_SET_TX,
    SX126X_CMD_SET_PACKET_TYPE, SX126X_CMD_GET_PACKET_TYPE,
    SX126X_CMD_SET_PACKET_PARAMS, SX126X_CMD_SET_DIO_IRQ_PARAMS,
    SX126X_CMD_CLEAR_IRQ_STATUS, SX126X_CMD_SET_BUFFER_BASE_ADDRESS,
    SX126X_CMD_SET_RX_TX_FALLBACK_MODE, SX126X_CMD_WRITE_REGISTER,
    SX126X_CMD_READ_REGISTER, SX126X_RX_TX_FALLBACK_MODE_FS,
    SX126X_PACKET_TYPE_LORA, SX126X_PACKET_TYPE_GFSK,
    SX126X_LORA_HEADER_EXPLICIT, SX126X_LORA_HEADER_IMPLICIT,
    ERR_NONE, ERR_SPI_CMD_FAILED,
)

# A status byte that is neither 0x00 nor 0xFF and whose command bits say nothing went wrong.
OK_STATUS = 0x24
# The same three bits set to "command failed", which the driver must still notice.
FAILED_STATUS = 0x2A


class FakeSPI:
    """Records every transfer, and answers reads from a tiny command model."""

    def __init__(self):
        self.transfers = []          # one entry per bus transaction, as bytes
        self.packet_type = SX126X_PACKET_TYPE_LORA
        self.status = OK_STATUS

    def _reply_for(self, out):
        reply = bytearray(len(out))
        for i in range(len(out)):
            reply[i] = self.status
        # GetPacketType answers with a status byte and then the type.
        if len(out) and out[0] == SX126X_CMD_GET_PACKET_TYPE and len(reply) > 1:
            reply[-1] = self.packet_type
        return reply

    def write_readinto(self, out, into):
        data = bytes(out)
        self.transfers.append(data)
        if data and data[0] == SX126X_CMD_SET_PACKET_TYPE and len(data) > 1:
            self.packet_type = data[1]
        into[:] = self._reply_for(data)

    def write(self, data):                # only the CircuitPython path uses this
        raise AssertionError("the micropython path should never write a byte at a time")


class FakePin:
    def __init__(self, value=False):
        self._v = value

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = v


class _Impl:
    name = "micropython"


class _CtorSPI:
    """Only has to let the constructor through; the bus the tests watch is attached after."""
    def __init__(self, *a, **k):
        pass


class _CtorPin:
    IN = 0
    OUT = 1

    def __init__(self, *a, **k):
        pass


@pytest.fixture
def radio(monkeypatch):
    """A driver instance wired to the fake bus.

    Under CPython neither of the driver's two platform branches runs, so __init__ leaves the
    pins and the bus unset and they are attached here. Everything below that point does take
    the micropython branch, because the module's `implementation` is replaced.
    """
    monkeypatch.setattr(driver, "implementation", _Impl)
    monkeypatch.setattr(driver, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.setattr(driver, "sleep_us", lambda us: None, raising=False)
    clock = [0]

    def tick():
        clock[0] += 1000            # a microsecond clock that advances a millisecond a read
        return clock[0]

    monkeypatch.setattr(driver, "ticks_ms", lambda: clock[0] // 1000, raising=False)
    monkeypatch.setattr(driver, "ticks_us", tick, raising=False)
    monkeypatch.setattr(driver, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.setattr(_sx126x, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.setattr(driver, "SPI", _CtorSPI, raising=False)
    monkeypatch.setattr(driver, "Pin", _CtorPin, raising=False)

    r = driver.SX126X(spi_bus=1, clk=9, mosi=10, miso=11, cs=8, irq=14, rst=12, gpio=13)
    r.spi = FakeSPI()
    r.cs = FakePin(True)
    r.gpio = FakePin(False)              # BUSY is never asserted
    r.irq = FakePin(True)                # DIO1 is already high, so a transmit completes at once
    r.rst = FakePin(True)

    # A plausible LoRa configuration, so the parts that compute airtime have numbers to use.
    r._modem = SX126X_PACKET_TYPE_LORA
    r._sf = 7
    r._bwKhz = 125.0
    r._bw = 4
    r._cr = 5
    r._ldro = 0
    r._crcType = 1
    r._preambleLength = 8
    r._headerType = SX126X_LORA_HEADER_EXPLICIT
    r._implicitLen = 0xFF
    r._invertIQ = 0
    return r


def opcodes(spi):
    return [t[0] for t in spi.transfers]


# ---------------------------------------------------------------- the crystal


def test_config_programmes_the_fs_fallback_and_not_a_standby(radio):
    """STDBY_RC stops the crystal, and a board with a TCXO pays to start it again.

    Measured on a T3-S3: one SetRx costs 8.0 ms coming out of STDBY_RC and 1.9 ms out of FS.
    That difference is larger than the whole time a peer takes to begin answering, so this
    single byte is the difference between a Hub that hears replies and one that does not.
    """
    radio.config(SX126X_PACKET_TYPE_LORA)

    fallback = [t for t in radio.spi.transfers if t[0] == SX126X_CMD_SET_RX_TX_FALLBACK_MODE]
    assert len(fallback) == 1, "the fallback mode should be set exactly once, at config time"
    assert fallback[0][1] == SX126X_RX_TX_FALLBACK_MODE_FS


def test_a_transmit_does_not_end_by_stopping_the_crystal(radio):
    """The fallback mode is undone by a standby, so the standby had to go with it.

    Leaving both in place measures slower than changing neither, because the chip enters FS
    and is immediately told to leave.
    """
    radio.transmit(b"a request", 9)

    # The standby the driver still does is the one at the start, before it sends.
    sent = opcodes(radio.spi)
    tx = sent.index(SX126X_CMD_SET_TX)
    assert SX126X_CMD_SET_STANDBY not in sent[tx:], \
        "a completed transmission must leave the synthesizer running"


def test_an_abandoned_transmit_still_parks_the_chip(radio):
    """A transmission that never completes is a different case: park it rather than leave it
    in FS with nobody planning to arm anything."""
    radio.irq.value(False)               # TX_DONE never arrives
    radio.transmit(b"a request", 9)

    sent = opcodes(radio.spi)
    tx = sent.index(SX126X_CMD_SET_TX)
    assert SX126X_CMD_SET_STANDBY in sent[tx:]


# ---------------------------------------------------------------- the rebuild


def test_resume_receive_restores_only_what_a_transmission_changed(radio):
    """Three commands, in this order, and nothing else.

    startReceive re-reads the packet type twice and rewrites the packet parameters twice. None
    of that changed while the radio was transmitting, and re-sending it was most of what kept
    this radio deaf.
    """
    radio.spi.transfers.clear()
    radio.resumeReceive()

    assert opcodes(radio.spi) == [SX126X_CMD_SET_DIO_IRQ_PARAMS,
                                  SX126X_CMD_CLEAR_IRQ_STATUS,
                                  SX126X_CMD_SET_RX]


def test_an_implicit_header_receiver_still_gets_the_full_rebuild(radio):
    """The guard, and it is not a formality.

    The one receive parameter a transmission does overwrite is the payload length, which
    startTransmit sets to the outgoing frame's. An explicit-header receiver ignores it and
    reads the length from the header. An implicit-header receiver does not: for it, skipping
    the rebuild would leave the radio expecting frames the size of the last thing it sent.
    """
    radio._headerType = SX126X_LORA_HEADER_IMPLICIT
    radio.spi.transfers.clear()
    radio.resumeReceive()

    assert SX126X_CMD_SET_PACKET_PARAMS in opcodes(radio.spi)


def test_a_modem_that_is_not_lora_gets_the_full_rebuild(radio):
    radio._modem = SX126X_PACKET_TYPE_GFSK
    radio.spi.transfers.clear()
    radio.resumeReceive()

    assert opcodes(radio.spi) != [SX126X_CMD_SET_DIO_IRQ_PARAMS,
                                  SX126X_CMD_CLEAR_IRQ_STATUS,
                                  SX126X_CMD_SET_RX]


def test_the_modem_the_guard_reads_is_the_one_config_set(radio):
    """The guard reads a cached value rather than asking the chip, because asking costs a
    round trip on the path this exists to shorten. So the cache has to be written."""
    radio._modem = 0
    radio.config(SX126X_PACKET_TYPE_LORA)

    assert radio._modem == SX126X_PACKET_TYPE_LORA


# ---------------------------------------------------------------- the bus


def test_a_command_is_one_transfer_rather_than_one_per_byte(radio):
    """A command used to cost a separate transfer, and a fresh allocation, for every byte."""
    radio.spi.transfers.clear()
    radio.setRx(0xFFFFFF)

    assert len(radio.spi.transfers) == 1
    assert radio.spi.transfers[0] == bytes([SX126X_CMD_SET_RX, 0xFF, 0xFF, 0xFF])


def test_a_write_puts_the_opcode_and_then_the_data_on_the_bus(radio):
    radio.spi.transfers.clear()
    radio.writeRegister(0x0740, [0x14, 0x24], 2)

    assert radio.spi.transfers[0] == bytes([SX126X_CMD_WRITE_REGISTER, 0x07, 0x40, 0x14, 0x24])


def test_a_read_clocks_a_nop_for_the_status_byte_and_one_for_every_byte_wanted(radio):
    """The extra NOP in front is the chip's status byte, and dropping it would shift every
    value read back by one."""
    data = bytearray(2)
    radio.spi.transfers.clear()
    radio.SPIreadCommand([SX126X_CMD_READ_REGISTER, 0x07, 0x40], 3, memoryview(data), 2)

    assert radio.spi.transfers[0] == bytes([SX126X_CMD_READ_REGISTER, 0x07, 0x40, 0, 0, 0])
    assert bytes(data) == bytes([OK_STATUS, OK_STATUS])


def test_a_chip_that_reports_a_failure_is_still_heard(radio):
    """The per-byte status decoding is the reason the old loop existed. Batching the transfer
    must not quietly drop it."""
    radio.spi.status = FAILED_STATUS

    assert radio.setRx(0xFFFFFF) == ERR_SPI_CMD_FAILED


def test_a_silent_bus_is_reported_rather_than_taken_as_success(radio):
    radio.spi.status = 0x00

    assert radio.setRx(0xFFFFFF) != ERR_NONE


# ---------------------------------------------------------------- the busy wait


def test_the_busy_wait_gives_up_the_cpu_when_the_chip_is_genuinely_slow(radio, monkeypatch):
    """A spin is right for a command that takes microseconds and wrong for a crystal start-up.

    Nothing on this board may hold the CPU for milliseconds, so the spin is bounded and the
    long waits fall through to a sleep exactly as they used to.
    """
    slept = []
    monkeypatch.setattr(_sx126x, "sleep_ms", lambda ms: slept.append(ms))

    ticks = [0]
    monkeypatch.setattr(driver, "ticks_ms", lambda: ticks[0], raising=False)

    class StuckBusy:
        def value(self):
            ticks[0] += 1
            return True

    radio.gpio = StuckBusy()

    assert radio._waitBusy(timeout=5) is False, "a chip that never answers has to time out"
    assert slept, "and the wait has to sleep rather than spin for the whole of it"


def test_the_busy_wait_does_not_sleep_when_the_chip_is_ready(radio, monkeypatch):
    slept = []
    monkeypatch.setattr(_sx126x, "sleep_ms", lambda ms: slept.append(ms))

    assert radio._waitBusy() is True
    assert slept == []

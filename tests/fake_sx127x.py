"""A fake SX1276, so the vendored SX127x driver can be driven without a radio.

The driver ships frozen into the firmware image and has no hardware-free rig of its own. This
models the chip at the register level, which is the only level the driver talks at, and it
models the three properties the driver's defects have actually turned on:

**Two register files.** Which file an address reaches depends on the LongRangeMode bit at the
moment of the transfer, so a modem write issued while the chip is still an FSK modem lands in
FSK space and never reaches the modem. A fake with one flat register space cannot see that
class of defect at all, because the lost write appears to land.

**Write-one-to-clear IRQ flags.** Writing a set bit to RegIrqFlags clears that flag and leaves
its neighbours alone. The receive path depends on this: clearing RxDone must not take the
payload-CRC flag down with it, or the flag would be gone before anyone could read it.

**RxDone fires whether or not the payload CRC checked.** The modem raises PayloadCrcError
*beside* RxDone rather than instead of it, and DIO0 is mapped to RxDone, so a damaged frame
wakes the receive path exactly like an intact one. That is why a receiver that never reads the
flag hands corruption to the application, and a fake that withheld RxDone on a CRC error would
make that defect untestable.

`FakeSpi.arrive()` is the whole interface a test needs: it stages a frame in the RX FIFO and
raises the flags the modem would raise for it.
"""
import os
import sys
import types

# The driver is vendored under firmware/, which is not on the package path.
_DRIVERS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "firmware", "drivers")
if _DRIVERS not in sys.path:
    sys.path.insert(0, _DRIVERS)

from PyLora_SX127x_extensions.board_config import BOARD          # noqa: E402
from PyLora_SX127x_extensions.constants import MASK, REG          # noqa: E402

FIFO = REG.LORA.FIFO
OP_MODE = REG.LORA.OP_MODE
FIFO_ADDR_PTR = REG.LORA.FIFO_ADDR_PTR
FIFO_RX_CURR_ADDR = REG.LORA.FIFO_RX_CURR_ADDR
IRQ_FLAGS = REG.LORA.IRQ_FLAGS
RX_NB_BYTES = REG.LORA.RX_NB_BYTES
MODEM_CONFIG_1 = REG.LORA.MODEM_CONFIG_1
MODEM_CONFIG_2 = REG.LORA.MODEM_CONFIG_2
INVERT_IQ = REG.LORA.INVERT_IQ

LONG_RANGE_MODE = 0x80
RX_PAYLOAD_CRC_ON = 0x04            # MODEM_CONFIG_2 bit 2: the modem computes the payload CRC

RX_DONE = 1 << MASK.IRQ_FLAGS.RxDone
PAYLOAD_CRC_ERROR = 1 << MASK.IRQ_FLAGS.PayloadCrcError
VALID_HEADER = 1 << MASK.IRQ_FLAGS.ValidHeader

# SX1276 reset defaults. The LoRa MODEM_CONFIG_1 default is the one that matters: 0x72 puts
# BW125 in the top nibble, which is why a lost bandwidth write looked like a working node.
LORA_DEFAULTS = {MODEM_CONFIG_1: 0x72, INVERT_IQ: 0x27}
FSK_DEFAULTS = {MODEM_CONFIG_1: 0x15, INVERT_IQ: 0x00}


class FakeSpi:
    """Two register files selected by LongRangeMode, plus an RX FIFO and latching IRQ flags.

    The driver's convention: address bit 7 set means write, clear means read.
    """

    def __init__(self):
        self.lora = dict(LORA_DEFAULTS)
        self.fsk = dict(FSK_DEFAULTS)
        self.lora_mode = False      # the chip comes out of reset as an FSK modem
        self.writes = []            # (register, value, lora_mode_at_the_time)
        self.fifo = b""             # what a read of RegFifo walks through
        self.fifo_ptr = 0

    def _file(self):
        return self.lora if self.lora_mode else self.fsk

    def transfer(self, address, value=0x00):
        if address & 0x80:
            register = address & 0x7F
            self.writes.append((register, value, self.lora_mode))
            if register == IRQ_FLAGS:
                # Write one to clear: a set bit in the written value takes that flag down and
                # a clear bit leaves it standing.
                self.lora[IRQ_FLAGS] = self.flags() & ~value & 0xFF
                return value
            if register == FIFO_ADDR_PTR:
                self.fifo_ptr = value
            self._file()[register] = value
            if register == OP_MODE:
                self.lora_mode = bool(value & LONG_RANGE_MODE)
            return value
        if address == FIFO:
            byte = self.fifo[self.fifo_ptr] if self.fifo_ptr < len(self.fifo) else 0x00
            self.fifo_ptr += 1
            return byte
        return self._file().get(address, 0x00)

    def writes_to(self, register):
        return [w for w in self.writes if w[0] == register]

    def flags(self):
        return self.lora.get(IRQ_FLAGS, 0)

    def arrive(self, frame, crc_error=False):
        """A frame lands: stage it in the FIFO and raise the flags the modem raises for it.

        RxDone goes up either way. That is the whole reason an unchecked receive path cannot
        tell a damaged frame from an intact one: the modem hands both of them over, and only
        PayloadCrcError beside it says which is which.
        """
        self.fifo = bytes(frame)
        self.fifo_ptr = 0
        self.lora[RX_NB_BYTES] = len(self.fifo)
        self.lora[FIFO_RX_CURR_ADDR] = 0
        raised = RX_DONE | VALID_HEADER | (PAYLOAD_CRC_ERROR if crc_error else 0)
        self.lora[IRQ_FLAGS] = self.flags() | raised

    def payload_crc_is_on(self):
        return bool(self.lora.get(MODEM_CONFIG_2, 0) & RX_PAYLOAD_CRC_ON)


class FakeBoard:
    """The board contract the driver uses: init_spi, set_irq_callbacks, get_spi, add_event_dio0."""

    last = None

    def __init__(self):
        self.spi = FakeSpi()
        self.cb_dio0 = None
        FakeBoard.last = self

    def init_spi(self):
        pass

    def set_irq_callbacks(self, cb_dio0=None, cb_dio1=None, cb_dio2=None, cb_dio3=None):
        self.cb_dio0 = cb_dio0

    def get_spi(self):
        return self.spi

    def add_event_dio0(self, value=None, blocked=None):
        """Wait on DIO0, as the real boards do: fire the callback on a rise, or time out.

        DIO0 is mapped to RxDone for a receive and stays asserted until RxDone is cleared, so
        the pin being high is exactly RxDone being set. A window that ends with the pin low
        raises, which is the "nothing arrived" path the connector turns into a timeout.
        """
        if not self.spi.flags() & RX_DONE:
            raise BOARD.LoRaTimeoutError("Timeout Exception!")
        self.cb_dio0(None)
        return 1


def _install_fake_board_module():
    """Point the driver's Raspberry Pi branch at the fake board.

    pyLora picks its board from `os.uname().machine`, which on a development machine matches
    none of its branches, so construction would die for want of a board. The RPi branch is the
    one to borrow: its real module imports RPi.GPIO and spidev, neither of which can import
    here, so standing in for it takes nothing away that was available.
    """
    name = "PyLora_SX127x_extensions.board_config_rpi"
    module = types.ModuleType(name)
    module.BOARD_RPI = FakeBoard
    sys.modules[name] = module


_install_fake_board_module()

from PyLora_SX127x_extensions.pyLora import pyLora      # noqa: E402


class FakePyLora(pyLora):
    # Take the RPi branch of the board selection, which now resolves to FakeBoard. Set on a
    # subclass rather than on pyLora so nothing else in a test run inherits it.
    IS_RPi = True


def build_pylora(**kwargs):
    """A real pyLora on a fake chip. Returns (radio, spi).

    Construction is the real one, not a shortcut past it: `__init__` is where rx_crc is
    enabled, so a test can check the modem is actually computing the CRC that the receive
    path reads.
    """
    radio = FakePyLora(verbose=False, do_calibration=False, **kwargs)
    return radio, FakeBoard.last.spi

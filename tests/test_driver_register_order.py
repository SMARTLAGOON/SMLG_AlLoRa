"""Construction order in the SX127x driver: modem registers only exist in LoRa mode.

`LoRa.__init__` used to write bandwidth, header mode and IQ inversion *before* it wrote the
LongRangeMode bit. Until that bit is set the chip is still an FSK modem, where 0x1D is
RegRxBw rather than MODEM_CONFIG_1 and 0x33 is RegNodeAdrs rather than INVERT_IQ, so all
three writes landed in the wrong register file and never reached the modem. Two of them were
no-ops by luck, because the LoRa reset defaults already said what they were trying to say.
The third was not: every node came up at the reset default of 125 kHz whatever bandwidth its
config asked for, and nothing anywhere reported it.

The driver ships frozen into the firmware image and has no hardware-free rig of its own, so
this drives it through a fake SPI. The fake models the one property that made the bug possible
and invisible: the chip has *two* register files, and which one an address reaches depends on
the LongRangeMode bit at the moment of the transfer. A fake with a single flat register space
cannot see this bug at all, because the lost write appears to land.

Note that the chip does not simply enter LoRa mode once. Construction dips back into FSK for
the calibration block and returns, so what matters is the mode at the instant of each write,
not whether LoRa mode was reached earlier.
"""
import os
import sys

import pytest

# The driver is vendored under firmware/, which is not on the package path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "firmware", "drivers"))

from PyLora_SX127x_extensions.LoRa import LoRa            # noqa: E402
from PyLora_SX127x_extensions.constants import BW, MODE, REG    # noqa: E402

OP_MODE = REG.LORA.OP_MODE                  # 0x01, and it is 0x01 in both register files
MODEM_CONFIG_1 = REG.LORA.MODEM_CONFIG_1    # 0x1D, RegRxBw while the chip is still FSK
INVERT_IQ = REG.LORA.INVERT_IQ              # 0x33, RegNodeAdrs while the chip is still FSK

LONG_RANGE_MODE = 0x80

# SX1276 reset defaults. The LoRa MODEM_CONFIG_1 default is the one that matters: 0x72 puts
# BW125 in the top nibble, which is exactly why a lost bandwidth write looked like a working
# node rather than a broken one.
LORA_DEFAULTS = {MODEM_CONFIG_1: 0x72, INVERT_IQ: 0x27}
FSK_DEFAULTS = {MODEM_CONFIG_1: 0x15, INVERT_IQ: 0x00}


class FakeSpi:
    """Two register files, selected by the LongRangeMode bit, as on the real chip.

    The driver's convention: address bit 7 set means write, clear means read.
    """

    def __init__(self):
        self.lora = dict(LORA_DEFAULTS)
        self.fsk = dict(FSK_DEFAULTS)
        self.lora_mode = False      # the chip comes out of reset as an FSK modem
        self.writes = []            # (register, value, lora_mode_at_the_time)

    def _file(self):
        return self.lora if self.lora_mode else self.fsk

    def transfer(self, address, value=0x00):
        if address & 0x80:
            register = address & 0x7F
            self._file()[register] = value
            self.writes.append((register, value, self.lora_mode))
            if register == OP_MODE:
                self.lora_mode = bool(value & LONG_RANGE_MODE)
            return value
        return self._file().get(address, 0x00)

    def writes_to(self, register):
        return [w for w in self.writes if w[0] == register]


class FakeBoard:
    """The board contract LoRa.__init__ uses: init_spi, set_irq_callbacks, get_spi."""

    last = None

    def __init__(self):
        self.spi = FakeSpi()
        FakeBoard.last = self

    def init_spi(self):
        pass

    def set_irq_callbacks(self, cb_dio0=None, cb_dio1=None, cb_dio2=None, cb_dio3=None):
        pass

    def get_spi(self):
        return self.spi


def _build(**kwargs):
    radio = LoRa(Board_specification=FakeBoard, verbose=False, do_calibration=False, **kwargs)
    return radio, FakeBoard.last.spi


@pytest.mark.parametrize("register, name", [(MODEM_CONFIG_1, "MODEM_CONFIG_1"),
                                            (INVERT_IQ, "INVERT_IQ")])
def test_modem_registers_are_only_written_while_in_lora_mode(register, name):
    _, spi = _build(signal_bandwidth=BW.BW250, sf=9)

    writes = spi.writes_to(register)
    assert writes, "{} was never configured at all".format(name)

    lost = [index for index, (_, _, in_lora) in enumerate(writes) if not in_lora]
    assert not lost, (
        "{} written {} time(s) while the chip was still an FSK modem (write #{}): those go to "
        "the FSK register file and never reach the modem".format(name, len(lost), lost))


def test_configured_bandwidth_actually_reaches_the_modem():
    # The regression proper, and the fielded symptom: a lost write leaves the LoRa reset
    # default of BW125 in place, so the node runs at 125 kHz and reports no error.
    _, spi = _build(signal_bandwidth=BW.BW250, sf=9)

    assert spi.lora[MODEM_CONFIG_1] >> 4 == BW.BW250


@pytest.mark.parametrize("bandwidth", [BW.BW125, BW.BW250, BW.BW500, BW.BW62_5])
def test_every_bandwidth_lands(bandwidth):
    _, spi = _build(signal_bandwidth=bandwidth, sf=9)

    assert spi.lora[MODEM_CONFIG_1] >> 4 == bandwidth


def test_coding_rate_and_header_mode_survive_the_bandwidth_write():
    # Bandwidth, coding rate and header mode share MODEM_CONFIG_1 under read-modify-write, so
    # moving the bandwidth write must not clobber the neighbours that were already correct.
    _, spi = _build(signal_bandwidth=BW.BW250, sf=9, cr=3)

    config = spi.lora[MODEM_CONFIG_1]
    assert config >> 4 == BW.BW250
    assert (config >> 1) & 0x07 == 3
    assert config & 0x01 == 0, "explicit header mode expected"


def test_the_radio_is_left_in_standby():
    # Unchanged behaviour, pinned because the reordering moves writes across mode changes.
    radio, _ = _build(signal_bandwidth=BW.BW125, sf=9)

    assert radio.mode == MODE.STDBY

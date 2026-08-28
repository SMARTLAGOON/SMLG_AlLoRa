"""The SX1262 target: a vendored driver, a build row, and a connector that actually drives it.

The `t3s3-sx1262` row sat commented out in the build matrix because the driver it names was in
no working tree. It is vendored now, and with it come three defects that only ever showed on
this radio and only in the field, because nothing here could run it:

**The schema's names never reached the chip.** The config file writes `bandwidth`, `coding_rate`
and `tx_power`; this connector read `bw`, `cr` and `power`, so every one of those settings fell
back to a default while the config that named them looked honoured.

**A coding rate was accepted and then not applied.** AlLoRa counts coding rates 1..4, the driver
wants the denominator 5..8, and it answers an out-of-range value with a return code rather than
an exception.

**Frequency and power had no setter at all**, so the base class's no-op ran and a retune was
reported as applied while the radio stayed where it was.
"""
import os
import subprocess
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGETS = os.path.join(_ROOT, "firmware", "targets")
_DRIVERS = os.path.join(_ROOT, "firmware", "drivers")


class FakeSX1262:
    """The driver's surface, at the level the connector talks to it."""

    def __init__(self, **pins):
        self.pins = pins
        self.begun = None
        self.frequency = None
        self.power = None
        self.sf = None
        self.bw = None
        self.cr = None
        self.sent = []

    def begin(self, **kwargs):
        self.begun = kwargs
        self.frequency = kwargs.get("freq")
        self.bw = kwargs.get("bw")
        self.sf = kwargs.get("sf")
        self.cr = kwargs.get("cr")
        self.power = kwargs.get("power")

    def setFrequency(self, freq, calibrate=True):
        self.frequency = freq

    def setOutputPower(self, power):
        self.power = power

    def setSpreadingFactor(self, sf):
        self.sf = sf

    def setBandwidth(self, bw):
        self.bw = bw

    def setCodingRate(self, cr):
        # The real driver refuses anything outside 5..8 with a return code and changes nothing.
        if not (5 <= cr <= 8):
            return -1
        self.cr = cr
        return 0

    def getRSSI(self):
        return -70

    def getSNR(self):
        return 9.5


_built = []


def _install_driver_stub():
    """Stand in for the frozen `sx1262` module and for `network`, neither of which is CPython."""
    module = types.ModuleType("sx1262")

    def factory(**pins):
        radio = FakeSX1262(**pins)
        _built.append(radio)
        return radio

    module.SX1262 = factory
    sys.modules.setdefault("sx1262", module)
    if "ubinascii" not in sys.modules:
        import binascii
        sys.modules["ubinascii"] = binascii
    if "network" not in sys.modules:
        network = types.ModuleType("network")
        network.STA_IF = 0

        class WLAN:
            def __init__(self, _iface):
                pass

            def active(self, _on):
                return None

            def config(self, _key):
                return b"\xaa\xbb\xcc\xdd\xee\xff"

        network.WLAN = WLAN
        sys.modules["network"] = network


_install_driver_stub()

from AlLoRa.Connectors.SX1262_connector import SX1262_connector  # noqa: E402

# Take the stubs back out. The connector module holds its own references to what it imported,
# so it keeps working, and no other test in the session inherits a `network` or a `ubinascii`
# that only exists on a board.
for _stub in ("sx1262", "network", "ubinascii"):
    sys.modules.pop(_stub, None)


def _connector(**overrides):
    _built.clear()
    connector = SX1262_connector()
    config = {
        "name": "S", "debug": False,
        "freq": 868, "sf": 7, "bandwidth": 125, "coding_rate": 1, "tx_power": 14,
        "min_timeout": 0.5, "max_timeout": 12, "timeout_delta": 1,
        "protocol_version": 3,
    }
    config.update(overrides)
    connector.config(config)
    return connector, _built[-1]


# -- the config file reaches the radio -----------------------------------------------------


def test_the_settings_the_schema_writes_reach_the_chip():
    _, radio = _connector(bandwidth=250, coding_rate=2, tx_power=20, sf=9, freq=915)
    assert radio.frequency == 915
    assert radio.bw == 250
    assert radio.sf == 9
    assert radio.power == 20
    # 2 in AlLoRa's counting is 4/6, which this driver calls 6.
    assert radio.cr == 6


def test_a_coding_rate_is_converted_to_the_denominator_the_driver_wants():
    assert SX1262_connector._driver_cr(1) == 5
    assert SX1262_connector._driver_cr(4) == 8
    # Already a denominator: left alone, so a config written the driver's way still works.
    assert SX1262_connector._driver_cr(5) == 5
    assert SX1262_connector._driver_cr(8) == 8


def test_a_coding_rate_change_is_applied_rather_than_silently_refused():
    connector, radio = _connector()
    connector.set_cr(3)
    assert connector.cr == 3
    assert radio.cr == 7   # not 3, which the driver would have rejected


def test_the_pins_still_come_out_of_the_connector_block():
    _, radio = _connector(clk=5, mosi=6, miso=3, cs=7, rst=8, irq=33, gpio=34, spi_bus=2)
    assert radio.pins == {"spi_bus": 2, "clk": 5, "mosi": 6, "miso": 3,
                          "cs": 7, "irq": 33, "rst": 8, "gpio": 34}


# -- the setters the base class leaves as no-ops -------------------------------------------


def test_a_retune_moves_the_radio_rather_than_only_the_bookkeeping():
    connector, radio = _connector()
    assert connector.change_rf_config(frequency=869, sf=9, bw=250, cr=2, tx_power=20) is True
    assert radio.frequency == 869
    assert radio.power == 20
    assert radio.sf == 9
    assert radio.bw == 250
    assert radio.cr == 6
    assert connector.get_rf_config() == [869, 9, 250, 2, 20]


def test_the_signal_readings_come_from_the_radio():
    connector, _ = _connector()
    assert connector.get_rssi() == -70
    # The base class answers 0 for a connector that does not override this, and a node that
    # reads 0 dB SNR on every frame cannot tell a good link from a marginal one.
    assert connector.get_snr() == 9.5


# -- the vendored driver and the build row -------------------------------------------------


def test_the_driver_is_vendored_where_the_build_row_names_it():
    for name in ("_sx126x.py", "sx126x.py", "sx1262.py"):
        assert os.path.isfile(os.path.join(_DRIVERS, "sx1262", name))


def test_the_sx1262_driver_is_a_flat_set_of_modules_not_a_package():
    """Which is why the build copies its files rather than its directory.

    `sx1262` imports `sx126x`, which imports `_sx126x`, all by bare name. Inside a package
    directory none of those resolve, so the driver has to land at the top of the frozen bundle.
    The SX127x driver is the other case: it has an `__init__.py` and is imported by its
    directory name, so it is copied as a directory.
    """
    assert not os.path.isfile(os.path.join(_DRIVERS, "sx1262", "__init__.py"))
    assert os.path.isfile(os.path.join(_DRIVERS, "PyLora_SX127x_extensions", "__init__.py"))
    source = open(os.path.join(_DRIVERS, "sx1262", "sx1262.py")).read()
    assert "from _sx126x import" in source and "from sx126x import" in source


def _matrix_rows():
    workflow = open(os.path.join(_ROOT, ".github", "workflows", "build-firmware.yml")).read()
    rows = []
    for block in workflow.split("- target: ")[1:]:
        target = block.split("\n", 1)[0].strip()
        if target.startswith("#"):
            continue
        driver = block.split("driver: ", 1)[1].split("\n", 1)[0].strip()
        board = block.split("board: ", 1)[1].split("\n", 1)[0].strip()
        rows.append((target, board, driver))
    return rows


def test_both_radios_are_built():
    targets = {target for target, _, _ in _matrix_rows()}
    assert targets == {"t3s3-sx127x", "t3s3-sx1262"}


def test_every_target_the_matrix_names_has_the_files_the_build_copies():
    for target, board, driver in _matrix_rows():
        assert os.path.isdir(os.path.join(_TARGETS, target, "boards", board)), target
        assert os.path.isdir(os.path.join(_TARGETS, target, "modules")), target
        assert os.path.isdir(os.path.join(_DRIVERS, driver)), driver
        for name in ("manifest.py", "mpconfigboard.h", "mpconfigboard.cmake", "sdkconfig.board"):
            assert os.path.isfile(os.path.join(_TARGETS, target, "boards", board, name)), name


_SHARED_BOARD_FILES = [
    "boards/ESP32_GENERIC_S3/manifest.py",
    "boards/ESP32_GENERIC_S3/mpconfigboard.h",
    "boards/ESP32_GENERIC_S3/mpconfigboard.cmake",
    "boards/ESP32_GENERIC_S3/sdkconfig.board",
    "modules/lora32.py",
    "modules/lilygo_oled.py",
    "modules/board/sd_manager.py",
    "modules/board/oled_screen.py",
    "modules/board/led_alive.py",
    "modules/board/uart_manager.py",
]


@pytest.mark.parametrize("relative", _SHARED_BOARD_FILES)
def test_the_two_targets_describe_the_same_board_identically(relative):
    """Both targets are a T3S3: same pins, same screen, same card slot, different radio.

    So every file that describes the *board* has to be the same in both, and the way to keep it
    that way is to fail here rather than to discover on a bench that one target's SD pin was
    corrected and the other's was not. Two lists describing one board will diverge; the only
    question is whether they diverge loudly.
    """
    sx127x = open(os.path.join(_TARGETS, "t3s3-sx127x", relative), "rb").read()
    sx1262 = open(os.path.join(_TARGETS, "t3s3-sx1262", relative), "rb").read()
    assert sx127x == sx1262, relative

"""Getting a freshly flashed board back, and what it costs the person waiting.

Measured on the bench on 2026-08-27, over five flashes: the board never came back on its own,
not once, even though the write already ends with `--after hard_reset`. A second, separate
`esptool` call brought it back every time it was tried. So the tap on RESET that the wizard has
always asked for is not covering an occasional miss -- it is load-bearing on this hardware, and
it is also avoidable.

The other half of the same bench finding is the silence. The wait was ten probes at a nominal
one second and took about 145 real seconds, because a probe against a port that enumerates and
answers nothing blocks for its whole timeout. An operator saw nothing at all for two and a half
minutes and then a failure, which is indistinguishable from a hang.

So: escalate cheapest-first, and say what is happening while it happens.
"""
import pytest

from tools.allora_provision.board import Board, BoardError
from tools.allora_provision.fleet import Fleet
from tools.allora_provision.result import Result
from tools.allora_provision.steps import POST_FLASH_PROBE, flash

PORT = "/dev/cu.usbmodem1101"
MAC = "9eeff0dc"


class Wire:
    """A board on the end of a pretend `mpremote`/`esptool`, that a flash takes down."""

    def __init__(self, comes_back=False, wake_works=True, mac=MAC):
        self.calls = []
        self.probe_timeouts = []
        self.alive = True
        self.mac = mac
        self.comes_back = comes_back
        self.wake_works = wake_works

    def run(self, argv, timeout=120, capture=True):
        self.calls.append(list(argv))
        joined = " ".join(argv)

        if argv[0].startswith("esptool"):
            if "write_flash" in argv:
                # The write ends in a hard reset the board does not necessarily act on.
                self.alive = self.comes_back
            elif "chip_id" in argv:
                self.alive = self.wake_works
            return 0, "", ""

        if "ALLORA_PROBE" in joined:
            self.probe_timeouts.append(timeout)
        if not self.alive:
            return 1, "", "could not enter raw repl"
        if "ALLORA_PROBE" in joined:
            return 0, "ALLORA_PROBE\n", ""
        if "network" in joined:
            return 0, "MAC:{}\n".format(self.mac), ""
        if "fs" in argv and "cat" in argv:
            return 1, "", "no such file"
        return 0, "", ""

    def esptool_calls(self):
        return [" ".join(argv) for argv in self.calls if argv[0].startswith("esptool")]

    def woken(self):
        return [c for c in self.esptool_calls() if "chip_id" in c]


@pytest.fixture(autouse=True)
def literal_ports(monkeypatch):
    """Only this port exists, whatever is plugged into the machine running the suite."""
    monkeypatch.setattr("tools.allora_provision.board.candidate_ports",
                        lambda globs=None: [PORT])
    monkeypatch.setattr("tools.allora_provision.steps.candidate_ports",
                        lambda globs=None: [PORT])


def _flash(wire, tmp_path, on_stall=None):
    image = tmp_path / "firmware.bin"
    image.write_bytes(b"\x00")
    board = Board(PORT, runner=wire, esptool="esptool.py", sleep=lambda _s: None)
    result = Result("edge")
    flash(board, Fleet(str(tmp_path / "fleet")), result, str(image), "edge",
          expected_mac=MAC, on_stall=on_stall)
    return board, result


def test_a_board_that_comes_back_by_itself_is_left_alone(tmp_path):
    """The wake is a recovery, not a ritual. A working board is not reset for the look of it."""
    wire = Wire(comes_back=True)
    board, result = _flash(wire, tmp_path)
    assert wire.woken() == []
    assert board.port == PORT


def test_a_silent_board_is_reset_over_the_wire_rather_than_by_a_finger(tmp_path):
    """The best human-in-the-loop step is the one that does not happen."""
    asked = []
    wire = Wire(comes_back=False, wake_works=True)
    board, result = _flash(wire, tmp_path, on_stall=lambda port: asked.append(port))
    assert len(wire.woken()) == 1
    assert asked == [], "the operator was asked for a tap the wire had already made unnecessary"
    assert any(step["step"] == "reboot" for step in result.steps)


def test_the_tap_is_asked_for_inside_the_run_and_the_flash_then_continues(tmp_path):
    """Today this is a failed run, a message and a re-run from the top. It is one prompt."""
    wire = Wire(comes_back=False, wake_works=False)
    asked = []

    def on_stall(port):
        asked.append(port)
        wire.alive = True          # the finger lands

    board, result = _flash(wire, tmp_path, on_stall=on_stall)
    assert asked == [PORT]
    assert len(wire.woken()) == 1, "the wire reset is still tried before a person is bothered"
    assert any(step["step"] == "reboot" for step in result.steps)


def test_with_nobody_at_the_keyboard_the_wait_still_refuses(tmp_path):
    """The non-interactive commands, and the website, keep the behaviour they had."""
    wire = Wire(comes_back=False, wake_works=False)
    with pytest.raises(BoardError) as raised:
        _flash(wire, tmp_path, on_stall=None)
    message = str(raised.value)
    assert "Tap RESET" in message
    assert "the flash itself completed" in message


def test_the_wait_says_what_it_is_waiting_for(tmp_path, capsys):
    """Two and a half minutes of nothing reads as a hang, and a hang is what people kill."""
    wire = Wire(comes_back=False, wake_works=True)
    _flash(wire, tmp_path)
    said = capsys.readouterr().out
    assert "waiting for the board to come back" in said
    assert "resetting it over the wire" in said


def test_a_probe_of_a_silent_port_is_not_given_the_full_fifteen_seconds(tmp_path):
    """The measured 145s was ten probes each blocking for its whole timeout, not ten seconds."""
    wire = Wire(comes_back=False, wake_works=True)
    _flash(wire, tmp_path)
    waiting = wire.probe_timeouts[-1]
    assert waiting == POST_FLASH_PROBE < 15

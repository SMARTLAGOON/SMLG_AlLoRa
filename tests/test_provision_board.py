"""Talking to a board, with a fake in place of the wire.

Everything the T3S3 makes painful is a native-USB consequence, and each one is a rule the
wizard has to encode rather than a note somebody has to remember: use `mpremote` and never
`ampy`; enter download mode over the wire because `esptool`'s auto-reset does not work; keep
the board in the loader between erase and write; expect the REPL to be unreachable straight
after a flash because the port re-enumerates.

The one rule with a security consequence is the last group: the identity key is generated on
the board, by the library's own code, and never minted on the laptop and pushed.
"""
import pytest

from tools.allora_provision.board import (
    Board, BoardError, candidate_ports, discover_boards)

PORT = "/dev/cu.usbmodem1101"
DEVICE_ID = "d909f4eb" + "00" * 28


class FakeRunner:
    """Records every command and answers from a table of canned replies."""

    def __init__(self, replies=None, default=(0, "", "")):
        self.calls = []
        self.replies = replies or []
        self.default = default

    def run(self, argv, timeout=120, capture=True):
        self.calls.append(list(argv))
        for match, reply in self.replies:
            if match(argv):
                return reply
        return self.default

    def commands(self):
        return [" ".join(argv) for argv in self.calls]


def _board(runner, sleeps=None):
    return Board(PORT, runner=runner, sleep=lambda _s: (sleeps or []).append(_s))


def _contains(*needles):
    return lambda argv: all(n in " ".join(argv) for n in needles)


# --- reaching the board ------------------------------------------------------------------

def test_every_file_operation_goes_through_mpremote_and_never_ampy(tmp_path):
    """`ampy` hangs on the USB-Serial/JTAG REPL, which looks like a dead board."""
    local = tmp_path / "LoRa.json"
    local.write_text("{}")
    runner = FakeRunner()
    board = _board(runner)
    board.write_remote(str(local), "LoRa.json")
    board.read_remote("identity.key")
    joined = " ".join(runner.commands())
    assert "ampy" not in joined
    assert runner.commands()[0].startswith("mpremote connect {} fs cp".format(PORT))
    assert runner.commands()[0].endswith(":LoRa.json")


def test_a_board_is_alive_only_when_the_repl_answers():
    answering = FakeRunner(default=(0, "ALLORA_PROBE\n", ""))
    silent = FakeRunner(default=(1, "", "could not enter raw repl"))
    assert _board(answering).alive() is True
    assert _board(silent).alive() is False


def test_discovery_probes_candidates_and_keeps_only_the_ones_that_answer(monkeypatch):
    """Other USB serial devices can be present and they are not boards, so a candidate port is
    never assumed to be one."""
    import tools.allora_provision.board as board_module
    monkeypatch.setattr(board_module.glob, "glob",
                        lambda _pattern: ["/dev/ttyACM0", "/dev/ttyACM1"])
    runner = FakeRunner(replies=[(_contains("/dev/ttyACM0"), (0, "ALLORA_PROBE\n", ""))],
                        default=(1, "", "no repl"))
    assert discover_boards(runner=runner, globs=("/dev/ttyACM*",)) == ["/dev/ttyACM0"]


def test_a_missing_tool_says_which_one_and_that_it_is_not_part_of_the_install():
    from tools.allora_provision.board import Runner
    with pytest.raises(BoardError) as excinfo:
        Runner().run(["definitely-not-a-real-tool-9f3a", "--help"])
    assert "definitely-not-a-real-tool-9f3a" in str(excinfo.value)
    assert "PATH" in str(excinfo.value)


def test_a_busy_port_is_reported_as_a_busy_port(monkeypatch):
    """This is the commonest bench failure and it reads like a dead board otherwise: a capture
    or a `screen` left open holds the device and every later step times out."""
    import subprocess
    from tools.allora_provision.board import Runner

    def timing_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", timing_out)
    with pytest.raises(BoardError) as excinfo:
        Runner().run(["mpremote", "connect", PORT, "fs", "ls"], timeout=5)
    assert "holding the port" in str(excinfo.value)


# --- the library the firmware carries ----------------------------------------------------

def test_this_library_passes_its_own_probe():
    """Run against the library in this tree, the probe must say current, or every board fails."""
    import contextlib
    import io
    from tools.allora_provision.board import _LAYOUT_SNIPPET
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(_LAYOUT_SNIPPET, {})
    assert _board(FakeRunner(default=(0, out.getvalue(), ""))).has_current_library() is True


def test_the_probe_refuses_the_library_as_built_before_data_source(tmp_path):
    """d768b21 has the 3.0 folders, but its Edge takes no data_source=, which the program passes."""
    import os
    import subprocess
    import sys
    from tools.allora_provision.board import _LAYOUT_SNIPPET
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    archive = subprocess.run(["git", "-C", root, "archive", "d768b21", "AlLoRa"],
                             capture_output=True)
    if archive.returncode != 0:
        pytest.skip("this checkout does not hold d768b21")
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=archive.stdout, check=True)
    # The old tree goes first on the path, so the probe cannot reach the live library instead.
    first = "import sys\nsys.path.insert(0, {!r})\n".format(str(tmp_path))
    probe = subprocess.run([sys.executable, "-I", "-c", first + _LAYOUT_SNIPPET],
                           capture_output=True, text=True)
    runner = FakeRunner(default=(probe.returncode, probe.stdout, probe.stderr))
    assert _board(runner).has_current_library() is False


def test_a_board_built_before_the_version_constant_does_not_have_the_current_library():
    """Builds up to the 3.0.0 release lack __version__, and their Edge takes no data_source=."""
    runner = FakeRunner(default=(1, "", "ImportError: can't import name __version__"))
    assert _board(runner).has_current_library() is False


def test_a_board_on_an_older_major_version_does_not_have_the_current_library():
    runner = FakeRunner(default=(0, "ALLORA_VERSION:2.9.0\n", ""))
    assert _board(runner).has_current_library() is False


def test_a_patch_release_counts_as_current():
    runner = FakeRunner(default=(0, "ALLORA_VERSION:3.0.1\n", ""))
    assert _board(runner).has_current_library() is True


def test_a_board_whose_import_fails_does_not_have_the_current_library():
    runner = FakeRunner(default=(1, "", "ImportError: no module named 'AlLoRa.Data'"))
    assert _board(runner).has_current_library() is False


def test_a_board_that_fails_for_another_reason_is_not_told_to_reflash():
    """A busy port is not old firmware, and reflashing would not fix it."""
    runner = FakeRunner(default=(1, "", "could not enter raw repl"))
    with pytest.raises(BoardError) as excinfo:
        _board(runner).has_current_library()
    assert "raw repl" in str(excinfo.value)


# --- identity ----------------------------------------------------------------------------

def test_the_identity_is_made_on_the_board_by_the_library_itself():
    """A device_id has to be something the board proves it holds. The wizard asks for one; it
    never mints a key on the laptop and pushes it."""
    runner = FakeRunner(default=(0, "DEVID:" + DEVICE_ID + "\n", ""))
    assert _board(runner).ensure_identity() == DEVICE_ID
    snippet = runner.commands()[0]
    assert "load_or_create_identity" in snippet
    assert "AlLoRa.Protocol.Security.identity" in snippet


def test_a_board_that_reports_no_device_id_points_at_the_firmware():
    """AlLoRa is frozen into the image, so a library change only reaches the board through a
    fresh build. That is the likely cause and the message has to say so."""
    runner = FakeRunner(default=(0, "nothing useful\n", ""))
    with pytest.raises(BoardError) as excinfo:
        _board(runner).ensure_identity()
    assert "firmware" in str(excinfo.value)


def test_reading_an_identity_that_is_not_there_is_not_an_error():
    """A board being flashed for the first time has none, and that is the normal case, not a
    failure: it means there is nothing to back up."""
    runner = FakeRunner(default=(1, "", "no such file"))
    assert _board(runner).identity_material() is None


def test_an_identity_that_is_there_comes_back_stripped():
    runner = FakeRunner(default=(0, "aa" * 32 + "\r\n", ""))
    assert _board(runner).identity_material() == "aa" * 32


# --- flashing ----------------------------------------------------------------------------

def test_download_mode_is_entered_over_the_wire_so_nobody_has_to_hold_buttons():
    runner = FakeRunner()
    _board(runner).enter_bootloader()
    assert "machine.bootloader()" in runner.commands()[0]
    assert runner.commands()[0].startswith("mpremote connect")


def test_erase_and_write_keep_the_board_in_the_loader_in_between():
    """A hard reset out of download mode between the two would need another BOOT hold, which
    is the whole thing entering the loader over the wire was meant to avoid."""
    runner = FakeRunner()
    _board(runner).flash("/tmp/firmware.bin")
    erase, write = runner.commands()
    assert "erase_flash" in erase and "--after no_reset" in erase
    assert "write_flash" in write
    assert "--before no_reset" in write and "--after hard_reset" in write
    assert write.endswith("/tmp/firmware.bin")
    assert "--chip esp32s3" in erase and "--chip esp32s3" in write


def test_a_flash_that_fails_tells_the_operator_the_button_fallback():
    runner = FakeRunner(default=(1, "", "Failed to connect"))
    with pytest.raises(BoardError) as excinfo:
        _board(runner).flash("/tmp/firmware.bin")
    assert "BOOT" in str(excinfo.value) and "RESET" in str(excinfo.value)


def test_the_repl_is_polled_after_a_flash_rather_than_assumed_back():
    """The write ends in a hard reset and the native-USB port re-enumerates, so `mpremote`
    reports no raw repl for a while afterwards."""
    attempts = {"n": 0}

    class _ComesBack(FakeRunner):
        def run(self, argv, timeout=120, capture=True):
            attempts["n"] += 1
            self.calls.append(list(argv))
            return (0, "ALLORA_PROBE\n", "") if attempts["n"] >= 3 else (1, "", "no raw repl")

    sleeps = []
    board = Board(PORT, runner=_ComesBack(), sleep=sleeps.append)
    assert board.wait_for_repl(attempts=5, delay=0.5) is True
    assert attempts["n"] == 3
    assert sleeps == [0.5, 0.5]


def test_a_board_that_never_comes_back_is_reported_rather_than_waited_on_forever():
    board = Board(PORT, runner=FakeRunner(default=(1, "", "no raw repl")), sleep=lambda _s: None)
    assert board.wait_for_repl(attempts=3, delay=0.01) is False


# --- running -----------------------------------------------------------------------------

def test_a_run_soft_resets_so_the_output_is_caught_from_the_first_line():
    """A serial capture cannot do this: a hard reset drops the port and it comes back under a
    new name, leaving the capture holding a device that no longer exists."""
    runner = FakeRunner(default=(0, "EDGE ready\n", ""))
    code, out, _ = _board(runner).run_script("/tmp/edge_run.py")
    assert code == 0 and "EDGE ready" in out
    assert runner.commands()[0] == "mpremote connect {} run /tmp/edge_run.py".format(PORT)


def test_candidate_ports_looks_where_these_boards_actually_appear():
    globs = candidate_ports(globs=("/dev/definitely-nothing-here-*",))
    assert globs == []


def test_a_read_that_failed_is_not_reported_as_a_file_that_is_not_there():
    """The distinction the identity backup rests on. `mpremote` exits non-zero either way, so
    a busy port would otherwise look exactly like a board that has never made a key, and the
    next step erases the flash on that reading."""
    absent = FakeRunner(default=(1, "", "OSError: [Errno 2] ENOENT"))
    assert _board(absent).read_remote("identity.key") is None

    busy = FakeRunner(default=(1, "", "could not enter raw repl: device busy"))
    with pytest.raises(BoardError) as excinfo:
        _board(busy).read_remote("identity.key")
    assert "could not read" in str(excinfo.value)

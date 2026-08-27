"""Which board the wizard talks to after a flash.

The rule under test is one sentence: a port is adopted because the board on it says it is the
board we flashed, never because it is the only one talking. The difference is invisible with a
single board connected and decisive with two, which is why this went unnoticed until a bench
session on 2026-08-27 flashed an Edge and provisioned the Hub instead, reporting every step ok.

The trap is worth stating plainly, because the intuition runs the wrong way. "Exactly one board
answers, so that is mine" sounds safe. But a board answers only once its REPL is back, and the
board that was just flashed is the one still coming up, so at that moment the single answering
board is very nearly guaranteed to be the *other* one.
"""
import pytest

from tools.allora_provision import steps
from tools.allora_provision.board import Board
from tools.allora_provision.steps import adopt_port

FLASHED = "/dev/cu.usbmodem1101"     # the board the operator named
BYSTANDER = "/dev/cu.usbmodem101"    # a second board, connected and minding its own business
WEBCAM = "/dev/cu.usbmodem508NTABKK4962"

FLASHED_MAC = "9eeff0dc"
BYSTANDER_MAC = "9eeff0e0"


class FakeWire:
    """Ports that answer, and what each says its MAC is. A port that is absent here is a port
    whose REPL does not answer, which is the state of a board still coming back from a flash."""

    def __init__(self, macs, candidates=None):
        self.macs = macs
        self.candidates = candidates if candidates is not None else list(macs)
        self.probed = []

    def run(self, argv, timeout=120, capture=True):
        joined = " ".join(argv)
        port = next((p for p in self.candidates if p in joined), None)
        if port not in self.macs:
            return 1, "", "could not enter raw repl"
        if "ALLORA_PROBE" in joined:
            return 0, "ALLORA_PROBE\n", ""
        if "network" in joined:
            self.probed.append(port)
            return 0, "MAC:{}\n".format(self.macs[port]), ""
        return 0, "", ""


@pytest.fixture(autouse=True)
def literal_ports(monkeypatch):
    """Take `globs` as the list of ports itself, instead of hitting the real /dev.

    Without this the suite passes or fails depending on what happens to be plugged into the
    machine running it, which for a test about which board is which would be its own joke.
    """
    def _literal(globs=None):
        return sorted(globs or ())

    monkeypatch.setattr("tools.allora_provision.board.candidate_ports", _literal)
    monkeypatch.setattr("tools.allora_provision.steps.candidate_ports", _literal)


def _adopt(wire, expected_mac, previous=FLASHED):
    return adopt_port(previous, runner=wire, expected_mac=expected_mac,
                      globs=tuple(wire.candidates))


# --- the bug, pinned ----------------------------------------------------------------------

def test_a_silent_flashed_board_does_not_hand_the_session_to_the_other_one():
    """The regression. Only the bystander answers, so the old count rule saw exactly one board
    and took it. Everything after the flash then ran against the wrong hardware."""
    wire = FakeWire({BYSTANDER: BYSTANDER_MAC}, candidates=[FLASHED, BYSTANDER])
    assert _adopt(wire, FLASHED_MAC) is None


def test_the_board_is_found_wherever_it_re_enumerated_to():
    """The case adoption exists for: the flashed board came back on a different path. With a
    MAC to match, a second connected board no longer makes this ambiguous."""
    wire = FakeWire({BYSTANDER: BYSTANDER_MAC, "/dev/cu.usbmodem102": FLASHED_MAC},
                    candidates=[FLASHED, BYSTANDER, "/dev/cu.usbmodem102"])
    assert _adopt(wire, FLASHED_MAC) == "/dev/cu.usbmodem102"


def test_the_previous_port_is_checked_first_and_costs_one_probe():
    """Re-enumeration is the exception, so the common case must not probe every board on the
    desk. It also must not be a special case that skips the check."""
    wire = FakeWire({FLASHED: FLASHED_MAC, BYSTANDER: BYSTANDER_MAC},
                    candidates=[FLASHED, BYSTANDER])
    assert _adopt(wire, FLASHED_MAC) == FLASHED
    assert wire.probed == [FLASHED]


def test_a_board_squatting_the_old_path_is_not_mistaken_for_ours():
    """The previous port is a hint, not an identity. If the path came back attached to another
    board, matching the MAC is what notices."""
    wire = FakeWire({FLASHED: BYSTANDER_MAC}, candidates=[FLASHED, BYSTANDER])
    assert _adopt(wire, FLASHED_MAC) is None


# --- no MAC to match on -------------------------------------------------------------------

def test_without_a_mac_a_lone_board_on_a_lone_port_is_still_adopted():
    wire = FakeWire({BYSTANDER: BYSTANDER_MAC}, candidates=[BYSTANDER])
    assert adopt_port(FLASHED, runner=wire, globs=(BYSTANDER,)) == BYSTANDER


def test_without_a_mac_a_second_candidate_port_is_enough_to_refuse():
    """A candidate is anything shaped like a board, answering or not. An unrelated USB serial
    device is enough to make the wizard ask rather than guess, which is the right direction to
    fail in: the cost of asking is a keystroke, the cost of guessing is a reflashed board."""
    wire = FakeWire({BYSTANDER: BYSTANDER_MAC}, candidates=[BYSTANDER, WEBCAM])
    assert adopt_port(FLASHED, runner=wire, globs=(BYSTANDER, WEBCAM)) is None


# --- the same rule, one level up ----------------------------------------------------------

def test_a_reachable_repl_on_the_right_port_is_not_enough_on_its_own(monkeypatch):
    """`wait_for_repl` answers "is anyone there", which was being read as "is it you". A port
    that answers with the wrong MAC has to fall through to adoption like any other miss."""
    wire = FakeWire({FLASHED: BYSTANDER_MAC}, candidates=[FLASHED])
    board = Board(FLASHED, runner=wire)
    assert board.wait_for_repl() is True
    assert steps._is_expected(board, FLASHED_MAC) is False
    assert steps._is_expected(board, None) is True

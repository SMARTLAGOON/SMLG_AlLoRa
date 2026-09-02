"""Transport recovery: rebooting a USB adapter and reopening the link under a running node.

A tunnel splits a node across a wire: the logic-holder (a Raspberry Pi) runs the engine, keys
and sessions; the adapter (an ESP32) runs the radio. When the adapter wedges, the fix today is
to restart the whole logic-holder, which throws away the roster, the sessions and the file in
flight to recover a radio.

Rebooting only the adapter is strictly cheaper, and over a *GPIO* reset it already works: the
port belongs to the Pi, so pulsing RST leaves the file descriptor valid. Over USB it does not,
because the serial device *is* the ESP32: reset it and the CDC device re-enumerates, the
descriptor dies, and the path it comes back on may differ. So a USB adapter needs three things
a GPIO one never did, and they are what this file holds:

  * a reopen path on the link, with a bounded wait for re-enumeration and a clear give-up;
  * a resolver, because the board must be found by identity rather than by a path that moved;
  * the reset actually being *called* on a stall, instead of once at startup and never again.

Everything here runs on CPython with fake ports; the hardware pass is a Pi with a T3-S3 on USB.
"""
import pytest

from AlLoRa.Links.Serial_link import Serial_link
from AlLoRa.Connectors.Serial_connector import Serial_connector


class _FakePort:
    """A pyserial-shaped port over a fixed script of bytes, so a reopen can be observed."""

    def __init__(self, path):
        self.path = path
        self.written = bytearray()
        self._rx = bytearray()
        self.closed = False

    def feed(self, data):
        self._rx.extend(data)

    def write(self, data):
        self.written.extend(data)
        return len(data)

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def close(self):
        self.closed = True


class _Opener:
    """Stands in for `serial.Serial(...)`: records every path it was asked for.

    `fail_reopens` makes the first N *re*opens raise, which is what a device that has not
    finished enumerating does. The very first open always succeeds, because a link that could
    not be built in the first place is a different problem from one that has to be rebuilt.
    """

    def __init__(self, fail_reopens=0):
        self.paths = []
        self.ports = []
        self._fail_reopens = fail_reopens

    def __call__(self, path):
        self.paths.append(path)
        if 1 < len(self.paths) <= self._fail_reopens + 1:
            raise OSError("[Errno 2] could not open port {}".format(path))
        port = _FakePort(path)
        self.ports.append(port)
        return port


def _client(serial_port, opener, **kwargs):
    return Serial_link.client(serial_port, open_port=opener, **kwargs)


# --- the reopen path -------------------------------------------------------------------------

def test_reopen_rebuilds_the_port_so_the_link_works_again():
    opener = _Opener()
    link = _client("/dev/ttyACM0", opener)
    first = opener.ports[0]

    assert link.reopen() is True

    # A second, distinct port object: the descriptor the reset killed is gone, not reused.
    assert len(opener.ports) == 2
    assert opener.ports[1] is not first
    link.write_reply(b"AFTER")
    assert bytes(opener.ports[1].written).startswith(b"AFTER")


def test_reopen_closes_the_dead_descriptor():
    opener = _Opener()
    link = _client("/dev/ttyACM0", opener)

    link.reopen()

    # Leaking the old handle is how a Pi runs out of file descriptors after a day of resets.
    assert opener.ports[0].closed is True


def test_reopen_resolves_the_path_again_so_a_board_that_moved_is_found():
    # A native-USB board re-enumerates and may come back on a different path, so the caller
    # hands over a resolver (a MAC lookup) rather than a fixed string.
    paths = iter(["/dev/ttyACM0", "/dev/ttyACM1"])
    opener = _Opener()
    link = _client(lambda: next(paths), opener)

    assert opener.paths == ["/dev/ttyACM0"]
    assert link.reopen() is True
    assert opener.paths == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_reopen_waits_for_the_device_to_come_back():
    # esptool returns before the CDC device is enumerated, so the first opens fail on a board
    # that is about to be fine. A probe fired once and given up on would call that a failure.
    opener = _Opener(fail_reopens=3)
    link = _client("/dev/ttyACM0", opener)

    assert link.reopen(attempts=6, delay=0.01) is True
    assert len(opener.paths) == 5          # the first open, three refusals, then the board


def test_reopen_gives_up_and_says_so():
    opener = _Opener(fail_reopens=99)
    link = _client("/dev/ttyACM0", opener)

    # Bounded, and the give-up is a return value the caller can act on, never an exception
    # thrown up through a running node's visit loop.
    assert link.reopen(attempts=3, delay=0.01) is False
    assert len(opener.paths) == 4          # the first open, plus three attempts


def test_reopen_drops_whatever_was_buffered_before_the_reset():
    opener = _Opener()
    link = _client("/dev/ttyACM0", opener)
    link._buf.extend(b"HALF-A-FRAME")

    link.reopen()

    # Half a frame from before the reboot, glued to the first frame after it, parses as
    # neither. The board that wrote it no longer exists.
    assert bytes(link._buf) == b""


def test_a_link_with_no_recipe_reports_failure_rather_than_raising():
    # The bridge half is constructed directly around a port it does not own; it has nothing to
    # reopen and must say so plainly.
    link = Serial_link(_FakePort("/dev/null"))
    assert link.reopen() is False


def test_reopen_rebinds_the_availability_probe():
    # `__init__` picks in_waiting / any() once from the port it is given. A reopen that swapped
    # the port but kept the old probe would read from a closed descriptor forever.
    opener = _Opener()
    link = _client("/dev/ttyACM0", opener)
    link.reopen()
    # Fed *after* the reopen: bytes that were waiting before it belong to the board that went
    # away, and the reopen is right to drop them.
    opener.ports[1].feed(b"HELLO" + Serial_link.SENTINEL)

    assert link.read_request(timeout=1) == b"HELLO"


# --- the reset being called on a stall -------------------------------------------------------

class _DeadLink:
    """A link whose far side stopped answering: every rpc times out. Counts reopens."""

    def __init__(self, reopen_result=True):
        self.rpcs = 0
        self.reopens = 0
        self.reopen_result = reopen_result
        self.replies = []
        self.mac_reply = None

    def rpc(self, request, timeout=None):
        self.rpcs += 1
        # The identity check after a reopen is a real round trip on this same link, so it must
        # be answerable separately from the transport verbs that are timing out.
        if b'"mac"' in request:
            return self.mac_reply
        return self.replies.pop(0) if self.replies else None

    def reopen(self, **kwargs):
        self.reopens += 1
        return self.reopen_result

    def close(self):
        pass


def _connector(link, **kwargs):
    connector = Serial_connector(**kwargs)
    connector.link = link
    return connector


def test_a_stalled_link_resets_the_adapter_and_reopens():
    resets = []
    link = _DeadLink()
    connector = _connector(link, reset_function=lambda: resets.append(1))

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert resets == [1]
    assert link.reopens == 1


def test_one_timeout_does_not_reboot_the_adapter():
    # A radio window that came back empty is ordinary. Rebooting the adapter for it would turn
    # a normal quiet minute into a reset loop.
    resets = []
    link = _DeadLink()
    connector = _connector(link, reset_function=lambda: resets.append(1))

    connector.transmit(b"wire")

    assert resets == []
    assert link.reopens == 0


def test_a_good_reply_clears_the_failure_count():
    resets = []
    link = _DeadLink()
    connector = _connector(link, reset_function=lambda: resets.append(1))

    connector.transmit(b"wire")                       # fail 1
    link.replies = [b'{"ok": true}']
    connector.transmit(b"wire")                       # answered: the run is broken
    connector.transmit(b"wire")                       # fail 1 again, not 2

    assert resets == []


def test_recovery_does_not_fire_again_on_the_very_next_failure():
    # After a recovery the count starts over, so a link that is still dead gets the *next* full
    # run of failures before the adapter is reset a second time. Without this a wedged board is
    # reset on every single verb.
    resets = []
    link = _DeadLink()
    connector = _connector(link, reset_function=lambda: resets.append(1))

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")
    connector.transmit(b"wire")

    assert resets == [1]


def test_a_link_that_raises_counts_as_a_failure_and_recovers():
    # The failure this whole path exists for does not arrive as a timeout. A USB board takes
    # its serial device with it when it reboots, so the next read is an I/O error on a
    # descriptor that no longer refers to anything. Counting only silences would mean the
    # recovery never fires on the exact wiring it was written for.
    resets = []

    class _RaisingLink(_DeadLink):
        def rpc(self, request, timeout=None):
            self.rpcs += 1
            raise OSError("[Errno 6] Device not configured")

    link = _RaisingLink()
    connector = _connector(link, reset_function=lambda: resets.append(1))

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        assert connector.transmit(b"wire") is False

    assert resets == [1]
    assert link.reopens == 1


def test_the_port_is_released_before_the_board_is_reset():
    # A USB CDC device admits one process at a time, and the reset tool has to open that same
    # device to reach the ROM loader. Resetting while this link still holds the port fails with
    # the device busy, which the bench found on 2026-09-02 by doing exactly that.
    order = []

    class _OrderedLink(_DeadLink):
        def close(self):
            order.append("close")

        def reopen(self, **kwargs):
            order.append("reopen")
            return super().reopen(**kwargs)

    link = _OrderedLink()
    connector = _connector(link, reset_function=lambda: order.append("reset"))

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert order == ["close", "reset", "reopen"]


def test_recovery_without_a_reset_function_still_reopens():
    # A USB adapter has no RST wire, and a caller that did not pass one still deserves the
    # reopen: a board that rebooted on its own (a brown-out) comes back the same way.
    link = _DeadLink()
    connector = _connector(link)

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert link.reopens == 1


def test_a_reset_that_raises_does_not_take_the_node_down():
    # reset_function is caller-supplied: an RPi.GPIO call on a Pi, an esptool subprocess over
    # USB. Both can fail, and neither may kill the visit loop of a Hub that is otherwise fine.
    def boom():
        raise RuntimeError("esptool not found")

    link = _DeadLink()
    connector = _connector(link, reset_function=boom)

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert link.reopens == 1


# --- identity: the port that came back must be the board that went away ----------------------

def test_a_recovered_link_confirms_it_is_the_same_board():
    # The path may be reused by whatever else is on the bus. Talking a session's worth of
    # frames at the wrong board is worse than staying down, so recovery asks the bridge who it
    # is and compares against the MAC this connector has been addressing all along.
    link = _DeadLink()
    connector = _connector(link)
    connector.MAC = "9eeff0e0"
    link.mac_reply = b'{"mac": "9eeff0e0"}'

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert connector.link_recovered is True


def test_a_different_board_on_the_path_fails_the_recovery():
    link = _DeadLink()
    connector = _connector(link)
    connector.MAC = "9eeff0e0"
    link.mac_reply = b'{"mac": "4a274ae0"}'      # the Edge, not the Hub

    for _ in range(Serial_connector.LINK_FAILURES_BEFORE_RECOVERY):
        connector.transmit(b"wire")

    assert connector.link_recovered is False


# --- the USB reset itself --------------------------------------------------------------------

def test_usb_reset_issues_the_proven_esptool_call():
    from AlLoRa.Connectors.Serial_connector import usb_reset

    calls = []

    class _Result:
        returncode = 0

    def runner(argv, **kwargs):
        calls.append(argv)
        return _Result()

    assert usb_reset("/dev/ttyACM0", esptool="esptool", runner=runner, settle=0) is True
    # The call benched on 2026-09-02: uptime 49 318 235 ms to 4 726 ms, same unique_id, no RST
    # pin touched. Pinned here so a well-meaning edit cannot quietly change what ships.
    # No `--no-stub`, and that absence is load-bearing: benched on a Pi, the stubless reset
    # leaves the board in the ROM loader, serving nothing behind a port that opens fine.
    assert calls == [["esptool", "--port", "/dev/ttyACM0", "--after", "hard_reset", "chip_id"]]


def test_usb_reset_finds_esptool_under_either_of_its_names():
    # esptool ships as `esptool.py` and as `esptool` depending on the version installed. A
    # recovery path that insisted on one name would be dead on half the machines it runs on,
    # and the failure would only ever show up with a wedged board and no way back.
    from AlLoRa.Connectors.Serial_connector import resolve_esptool

    assert resolve_esptool(which=lambda n: n == "esptool") == "esptool"
    assert resolve_esptool(which=lambda n: n == "esptool.py") == "esptool.py"
    assert resolve_esptool(which=lambda n: None) == "esptool.py"   # something to complain about


def test_usb_reset_retries_a_board_that_was_not_ready():
    # A board that has just rebooted reappears in the host's device list before it will accept
    # a connection, so the first attempt lands in that window often enough that one try is a
    # coin flip rather than a reset. Benched on 2026-09-02: repeated calls at one board
    # alternated between working and failing to open the port at all.
    from AlLoRa.Connectors.Serial_connector import usb_reset

    codes = iter([1, 0])

    class _Result:
        def __init__(self, rc):
            self.returncode = rc

    def runner(argv, **kwargs):
        return _Result(next(codes))

    assert usb_reset("/dev/ttyACM0", runner=runner, settle=0) is True


def test_usb_reset_gives_up_after_its_attempts():
    from AlLoRa.Connectors.Serial_connector import usb_reset

    calls = []

    class _Result:
        returncode = 1

    def runner(argv, **kwargs):
        calls.append(argv)
        return _Result()

    assert usb_reset("/dev/ttyACM0", attempts=3, runner=runner, settle=0) is False
    assert len(calls) == 3


def test_usb_reset_reports_a_failed_call_rather_than_raising():
    from AlLoRa.Connectors.Serial_connector import usb_reset

    def runner(argv, **kwargs):
        raise OSError("esptool: command not found")

    assert usb_reset("/dev/ttyACM0", runner=runner, settle=0) is False

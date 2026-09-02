"""Serial_connector: a serial tunnel's logic-holder half.

Thin over Tunnel_connector: it is a split Connector whose transport verbs cross a UART to a
bridge (Adapter) running the radio. All the tunnel logic (exchange-routed matching, D↓/td↑
pacing, opaque wire so v2/v3-open/v3-secure all cross unchanged) lives in Tunnel_connector;
this only builds the concrete Serial_link client from the config and keeps the old constructor
so existing Hub examples import it unchanged. It replaces the previous ad-hoc
`S&W:`/`ACK:`/`Listen:` string protocol, which re-parsed the frame on the bridge and so only
ever spoke v2.

It also owns *recovery*, because a serial adapter is the one bridge that can be rebooted and
reopened: see `recover_link` below, and `usb_reset` for doing it without a reset wire.
"""
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Links.Serial_link import Serial_link
from AlLoRa import tunnel_codec
from AlLoRa.utils.debug_utils import print


# The address a Connector carries before it has learned the bridge radio's real one. Comparing
# against it would pass every board, so identity is only checked once there is one to check.
_PLACEHOLDER_MAC = "00000000"


# esptool has shipped under both names across its versions, and a machine may have either. The
# provisioning toolkit resolves the same pair for the same reason; a working install must not be
# rejected over its filename, least of all inside a recovery path.
ESPTOOL_NAMES = ("esptool.py", "esptool")


def resolve_esptool(which=None):
    """Whichever name esptool answers to on this host, or the first to complain about."""
    if which is None:
        import shutil
        which = shutil.which
    for name in ESPTOOL_NAMES:
        if which(name):
            return name
    return ESPTOOL_NAMES[0]


def usb_reset(port, esptool=None, timeout=60, settle=5, attempts=3, runner=None):
    """Reboot the adapter over its own USB, a drop-in for the GPIO `reset_esp32`.

    A board reached over USB has no reset wire to pulse, and on a native-USB part (the T3-S3,
    and every other ESP32-S3 / C3) there is no USB-to-serial chip whose DTR line could stand in
    for one. What does work is asking the ROM loader to reset the chip, which is what this call
    does. Benched on 2026-09-02: uptime went from 49 318 235 ms to 4 726 ms, the chip reported
    the same unique_id, and the REPL was answering four seconds later, with nothing touched but
    the cable.

    The settle is not optional. The call returns while the host is still enumerating the CDC
    device, so anything that looks at the port immediately reads a board that is about to be
    fine as a board that is gone.

    Nor is the retry. Measured on the bench on 2026-09-02: fired repeatedly at one board, the
    call alternated between working and failing to open the port at all, and the failures were
    the attempts that followed a reset rather than the ones that followed a long wait. A board
    that has just rebooted reappears in the host's device list before it will accept a
    connection, so the first attempt lands in that window often enough that a single try is not
    a reset, it is a coin flip. The provisioning toolkit reached the same conclusion from the
    other side and waits the same way in `wait_for_repl`.

    This reaches every failure where the chip is still on the bus: hung code, a board sitting
    below MicroPython. It cannot reach the chip *leaving* the bus, which is deep sleep with USB
    powered down, a brown-out, or a wedged USB stack. Only a reset wire or a power cycle reaches
    those, so keep the wire wherever a power cycle is expensive to arrange.

    `runner` is the subprocess call, injectable so this is testable without a board.
    """
    if runner is None:
        import subprocess

        def runner(argv, **kwargs):
            return subprocess.run(argv, capture_output=True, **kwargs)

    import time

    argv = [esptool or resolve_esptool(), "--port", port, "--after", "hard_reset", "chip_id"]
    for attempt in range(max(1, attempts)):
        try:
            result = runner(argv, timeout=timeout)
            returncode = result.returncode
        except Exception:
            # esptool missing, the port already gone, the call timing out: all of them mean
            # this attempt did not reset the board, and none of them may take down a node that
            # is otherwise running.
            returncode = 1
        if settle:
            time.sleep(settle)
        if returncode == 0:
            return True
    return False


class Serial_connector(Tunnel_connector):

    def __init__(self, reset_function=None, rpc_timeout=20, link_margin=5,
                 reopen_attempts=10, reopen_delay=1.0, port_resolver=None):
        super().__init__(rpc_timeout=rpc_timeout, link_margin=link_margin)
        # Called when the link has gone quiet, not only once at startup as the old examples
        # did. Either shape fits: a GPIO pulse on a Pi's own wiring, or `usb_reset` above.
        self.reset_function = reset_function
        self._reopen_attempts = reopen_attempts
        self._reopen_delay = reopen_delay
        # Where the adapter is *now*, for a board whose path is not stable. A config file can
        # only hold a string, and a string is the right answer for a wire that cannot move (a
        # Pi's own UART) and the wrong one for a USB board, which comes back from a reboot on
        # whatever node the host next hands out. Callable and caller-supplied for the same
        # reason `reset_function` is: what identifies a board is the host's business, not the
        # protocol's. When given, it wins over the config's `serial_port`.
        self.port_resolver = port_resolver

    def config(self, config_json):
        if config_json:
            self.link = Serial_link.client(
                self.port_resolver or config_json.get('serial_port', "/dev/ttyAMA3"),
                baud=config_json.get('baud', 9600),
                timeout=config_json.get('timeout', 1))
        super().config(config_json)

    def recover_link(self):
        """Reboot the adapter and rebuild the link under a node that keeps running.

        Worth spelling out what this does *not* cost. The adapter holds a radio and nothing
        else: the sessions, the endpoint roster, the keys, the counters and the file in flight
        all live here on the logic-holder and survive untouched. So the node loses a few seconds
        of radio and keeps everything it knows, where restarting the process to fix the same
        wedge would throw all of it away.

        Four steps, in an order the bench had to teach us. Release the port *first*: a USB CDC
        device admits one process at a time, and `usb_reset` has to open that same device to
        reach the ROM loader, so a reset attempted while this link still holds the port fails
        with the device busy and the recovery silently does nothing. Then reset the board; then
        reopen, because over USB the serial device *is* the board and its descriptor died with
        it; then check that the board now on that path is the same board. A path is only a
        number the host hands out, and there is usually more than one device on the bus, so a
        reopen that skipped the check could put a session's worth of frames into the wrong board
        and read its silence as a bad radio link.

        On a GPIO wiring the first step costs nothing: the port belongs to the host, so closing
        and reopening it is a no-op either way.
        """
        self.link.close()
        if self.reset_function is not None:
            try:
                self.reset_function()
            except Exception as e:
                # A caller-supplied call: an RPi.GPIO pulse, an esptool subprocess. Both can
                # fail, and the reopen below is still worth trying — a board that rebooted on
                # its own comes back the same way.
                if self.debug:
                    print("Adapter reset raised: {}".format(e))
        if not self.link.reopen(attempts=self._reopen_attempts, delay=self._reopen_delay):
            return False
        return self._same_board()

    def _same_board(self):
        """True when the reopened link reaches the radio this connector has been addressing.

        Asked straight down the link rather than through `request_mac`, which adopts whatever
        it is told and rebuilds the codec around it. Here the whole point is to compare, not to
        adopt: a different board answering is the failure being tested for.
        """
        if not self.MAC or self.MAC == _PLACEHOLDER_MAC:
            return True                  # nothing learned yet, so nothing to contradict
        reply = self.link.rpc(tunnel_codec.encode_get_mac(), timeout=self._rpc_timeout)
        if not reply:
            return False                 # reopened onto something that does not speak the tunnel
        return tunnel_codec.decode_get_mac_reply(reply) == self.MAC

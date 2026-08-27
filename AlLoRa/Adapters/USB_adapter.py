"""USB_adapter: a bridge board that tunnels over its own USB socket.

Thin over Adapter, the same way Serial_adapter and WiFi_adapter are: the bridge holds the radio
but no protocol logic, no files, no sessions and no keys. It reads a transport-verb request,
runs that verb on its real radio Connector, and writes the result back. What it adds is the two
things a board whose console *is* its data port has to get right.

    adapter = USB_adapter(SX127x_connector(), "LoRa.json")
    adapter.run()

Plug the board into a Raspberry Pi (or any host) with one USB cable and it is a LoRa modem for
that host. There are no pins to wire, no baud to agree on, and nothing to configure in the
`adapter` block: USB-CDC has no such settings, and it runs at USB speed rather than at the
9600 baud a GPIO tunnel settles for, which takes the link almost entirely out of the transfer
time.

**Which boards need this one.** A board whose USB socket is *native* USB has no UART behind that
socket, so this is the only way to reach it over the cable. The T3S3 is that kind, and so are the
other ESP32-S3, ESP32-C3 and RP2040 boards, though the T3S3 is the one this has been run on. A board whose socket goes through a USB-to-serial chip (a LoPy4, an E5 on a
Grove-to-USB adapter) already presents a real UART to its own firmware: that is `Serial_adapter`
with UART0, and it needs nothing new. Both look identical from the host, which opens a serial
device by path either way.

**Why the logging moves.** On this board stdout is the wire. A debug line does not clutter a
log, it lands inside a frame and corrupts it, and the loudest logger on a bridge is not the
Adapter but the radio Connector under it. So the library's whole debug output is redirected at
construction, before anything can log, and `"log": "file"` is offered for a bench session. It is
off by default because the right amount of writing to a board's flash during a transfer is none.

The other half of the problem, the boot banner the board emits before any of this code runs,
cannot be redirected from here and is not tried: `Serial_link._resync` drops it on the host side,
where it arrives.
"""
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Links.Serial_link import Serial_link
from AlLoRa.utils import debug_utils


class _File_sink:
    """Append the library's debug lines to a file on the board instead of to the wire.

    Opened and closed per line: a bridge that loses power mid-session then costs the last line
    rather than the log, and nothing is holding a file handle open across a transfer. Keyword
    arguments to `print` (`end=''` and friends) are dropped, so a progress line that was written
    to build up on one console row arrives here as one row per call.
    """

    def __init__(self, path):
        self.path = path

    def __call__(self, *args, **kwargs):
        try:
            with open(self.path, "a") as f:
                f.write(" ".join(str(a) for a in args))
                f.write("\n")
        except Exception:
            # Logging must never be the thing that stops a bridge serving the radio.
            pass


class USB_adapter(Adapter):

    def __init__(self, connector=None, config_file=None, link=None, debug=False):
        # The sink goes in before the base constructor, not in setup_link, because boot()
        # announces the radio's MAC well before it reaches setup_link and on this board that
        # line would be written straight into the tunnel. Silence first, refine once the config
        # is read: that leaves no window in which a log line can reach the wire.
        debug_utils.set_sink(debug_utils.silence)
        super().__init__(connector, config_file=config_file, link=link, debug=debug)

    def setup_link(self, config):
        """Take the console as the link, unless the caller already handed one over.

        A link supplied to the constructor wins. The console is the only USB endpoint a stock
        board has, but it is not the only one a board can have: a MicroPython build that
        publishes a second CDC interface can keep the REPL on the first and give the tunnel the
        second, and it does that by building its own port and passing it in. Honouring that here
        is also what lets the whole path be exercised off-device.
        """
        self._setup_logging(config)
        if self.link is None:
            self.link = Serial_link.bridge_usb(
                text_safe=config.get('text_safe', True),
                poll_ms=config.get('poll_ms', 5))

    def _setup_logging(self, config):
        """Where this board's own debug output goes, now that it cannot go to stdout."""
        if config.get('log', 'off') == 'file':
            debug_utils.set_sink(_File_sink(config.get('log_path', 'adapter.log')))
        else:
            debug_utils.set_sink(debug_utils.silence)

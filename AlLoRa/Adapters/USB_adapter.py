"""An Adapter for boards with a native USB port, such as the T3S3 (ESP32-S3).

On these boards the USB console is the data link, so the library's debug output is switched off.
Set `"log": "file"` to write it to flash instead. Boards with a USB-serial chip use Serial_adapter.
Usage: `USB_adapter(SX127x_connector(), "LoRa.json").run()`.
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

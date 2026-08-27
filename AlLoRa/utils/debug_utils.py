"""debug_utils: the library's one debug print, and where its output is allowed to go.

Every AlLoRa module logs through the `print` below, shadowing the builtin, so a timestamp is
free and there is a single place that decides where a debug line ends up. That single place is
what makes a USB bridge possible at all: on a board whose tunnel runs over its own USB-CDC
console, stdout *is* the wire, and a stray debug line does not merely clutter a log, it lands
inside a frame and corrupts it. `set_sink` moves the library's output off that wire in one
call, covering not just the Adapter but the radio Connector underneath it, which is the noisiest
logger on a bridge board.

The sink is module-global on purpose. A per-object logger would have to be threaded through
Connector, Adapter and every DataSource to reach the call sites that matter, and a single one
of them missed is a corrupted frame; a board either has a free console or it does not, and that
is a property of the board, not of one object on it.
"""
import builtins
from AlLoRa.utils.time_utils import get_current_timestamp

# None means stdout, which is what every board with a free REPL wants and what the library has
# always done. Anything else is a callable taking (timestamp, *args, **kwargs).
_sink = None


def set_sink(sink):
    """Send every AlLoRa debug line to `sink(timestamp, *args, **kwargs)` instead of stdout.

    `None` restores stdout. Pass `silence` to drop the lines entirely, which is the safe
    default for a board whose stdout carries data.
    """
    global _sink
    _sink = sink


def get_sink():
    """The sink currently installed, or None for stdout. Mostly so a caller can put back what
    it found instead of assuming the default."""
    return _sink


def silence(*args, **kwargs):
    """A sink that drops the line, named so the call site reads as `set_sink(silence)`."""
    pass


def print(*args, **kwargs):
    timestamp = get_current_timestamp()
    if _sink is None:
        builtins.print(timestamp, *args, **kwargs)
    else:
        _sink(timestamp, *args, **kwargs)

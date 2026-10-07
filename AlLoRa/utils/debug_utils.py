"""The library's debug print. Every AlLoRa module uses it instead of the built-in `print`.

`set_sink` redirects all debug output at once. Boards whose console is the data link need this.
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

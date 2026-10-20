"""The log formatter ends every line with exactly one line break, whatever the format ends with."""
import io
import contextlib

from AlLoRa.Management.Logger import Handler, ULogger, INFO


def _logged(fmt):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ULogger("n", handlers=[Handler(level=INFO, colorful=False, fmt=fmt)]).info("hello")
    return out.getvalue()


def test_a_format_without_a_line_break_gets_one():
    assert _logged("%(level)s %(msg)s") == "INFO hello\n"


def test_a_format_that_already_ends_in_a_line_break_is_kept_as_written():
    # It used to lose the whole template here, and the first log line raised a TypeError.
    assert _logged("%(level)s %(msg)s\n") == "INFO hello\n"

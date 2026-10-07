"""Every module header in AlLoRa/, examples/, tools/ and firmware/targets/ stays short: at most
five lines of text.

The header says what the file is and anything that would look wrong without it. The design
reasoning lives outside the code, so a header that grows past this is the place it leaks back.
"""
import ast
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parent.parent
_ROOTS = ("AlLoRa", "examples", "tools", "firmware/targets")
_MAX_LINES = 5


def _header_lines(source):
    # A leading `#` block counts as header too, so the limit cannot be dodged by moving the
    # text out of the docstring. A `#!` line is not header text.
    comments = 0
    for line in source.splitlines():
        if line.startswith("#!"):
            continue
        if line.startswith("#"):
            comments += 1
        elif line.strip():
            break
    doc = ast.get_docstring(ast.parse(source))
    return comments + (len(doc.splitlines()) if doc else 0)


_FILES = sorted(p for root in _ROOTS for p in (_REPO / root).rglob("*.py"))


def test_the_library_is_found():
    assert sum(1 for p in _FILES if p.is_relative_to(_REPO / "AlLoRa")) > 50


@pytest.mark.parametrize("path", _FILES, ids=lambda p: str(p.relative_to(_REPO)))
def test_module_header_is_at_most_five_lines(path):
    lines = _header_lines(path.read_text())
    assert lines <= _MAX_LINES, f"{path.name}: header is {lines} lines, limit {_MAX_LINES}"

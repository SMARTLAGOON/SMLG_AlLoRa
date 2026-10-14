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


# Inside the code, a comment block or a function or class docstring stays at three lines: what
# the code does and at most one sentence of why. AlLoRa/ and tools/ join once they are cut.
_BODY_ROOTS = ("examples", "firmware/targets")
_BODY_MAX_LINES = 3
_BODY_FILES = sorted(p for root in _BODY_ROOTS for p in (_REPO / root).rglob("*.py"))


def _long_body_text(source):
    """Yield (line, length) for each comment block or inner docstring over the limit."""
    lines = source.splitlines()
    start = 0
    while start < len(lines) and (lines[start].startswith("#") or not lines[start].strip()):
        start += 1  # the module header is the test above's job
    run = 0
    for number, line in enumerate(lines[start:], start + 1):
        if line.strip().startswith("#"):
            run += 1
            continue
        if run > _BODY_MAX_LINES:
            yield number - run, run
        run = 0
    if run > _BODY_MAX_LINES:
        yield len(lines) - run + 1, run
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = [l for l in (ast.get_docstring(node) or "").splitlines() if l.strip()]
            if len(text) > _BODY_MAX_LINES:
                yield node.lineno, len(text)


@pytest.mark.parametrize("path", _BODY_FILES, ids=lambda p: str(p.relative_to(_REPO)))
def test_comments_and_docstrings_are_at_most_three_lines(path):
    found = list(_long_body_text(path.read_text()))
    assert not found, f"{path.name}: blocks over {_BODY_MAX_LINES} lines at (line, length) {found}"

"""The README names files, config keys and methods; each one must exist in this repository.

This catches a rename or a deletion that the README did not follow. It cannot tell whether a
sentence about behaviour is true: that still needs a reader with the code open.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")

# Called in the README as formulas, not as functions of this library.
NOT_LIBRARY = {"sha256", "SHA256"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _library_source():
    parts = []
    for folder in ("AlLoRa", os.path.join("examples", "v3_hello")):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            parts += [_read(os.path.join(dirpath, n)) for n in files if n.endswith(".py")]
    return "\n".join(parts)


def missing_paths(text):
    """Relative links and image sources that point at nothing."""
    refs = re.findall(r'\]\(([^)#\s]+)\)', text) + re.findall(r'src="([^"]+)"', text)
    local = [r for r in refs if not r.startswith(("http://", "https://"))]
    return sorted(r for r in set(local) if not os.path.exists(os.path.join(ROOT, r.replace("%20", " "))))


def missing_methods(text, source):
    """`name(...)` in backticks with no `def name` anywhere in the library."""
    named = set(re.findall(r'`(?:[\w.]+\.)?(\w+)\(', text)) - NOT_LIBRARY
    return sorted(n for n in named if not re.search(r'\bdef %s\(' % n, source))


def missing_config_keys(text, source):
    """Keys of the JSON examples that no code reads."""
    keys = set()
    for block in re.findall(r'```json\n(.*?)```', text, re.S):
        keys |= set(re.findall(r'"(\w+)"\s*:', block))
    return sorted(k for k in keys if not re.search(r'["\']%s["\']' % k, source))


def test_every_linked_file_exists():
    assert missing_paths(_read(README)) == []


def test_every_named_method_exists():
    assert missing_methods(_read(README), _library_source()) == []


def test_every_example_config_key_is_read():
    assert missing_config_keys(_read(README), _library_source()) == []


def test_the_checks_go_red_on_a_stale_readme():
    stale = ('[gone](AlLoRa/Nodes/Gateway.py) `hub.check_everything()`\n'
             '```json\n{ "no_such_key": 1 }\n```\n')
    source = _library_source()
    assert missing_paths(stale) == ["AlLoRa/Nodes/Gateway.py"]
    assert missing_methods(stale, source) == ["check_everything"]
    assert missing_config_keys(stale, source) == ["no_such_key"]

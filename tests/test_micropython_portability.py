"""Guard — the on-device secure path must not use CPython-only APIs.

The test suite runs on CPython, whose stdlib and builtins are a superset of MicroPython's, so a
CPython-only call passes CI and then throws (or silently degrades secure mode) on the ESP32. The
v3 secure bring-up hit four of these one reflash at a time: ``import ucryptolib`` (renamed to
``cryptolib``), ``hmac.compare_digest``, ``hashlib….digest_size``, and ``int.bit_length()``. This
test scans the files that get frozen into the firmware and fails in CI on the ones we know about,
so the ESP32 stops being the thing that finds them.

It looks at *code only* — comments and string/doc literals are stripped with ``tokenize`` — so the
inline explanations of these very fixes (which necessarily name the banned APIs) don't trip it.
"""
import os
import tokenize

# Files frozen into the firmware and reached while running a secure node.
_ON_DEVICE_SECURE = [
    "AlLoRa/Security/ec_p256.py",
    "AlLoRa/Security/kdf.py",
    "AlLoRa/Security/AEAD.py",
    "AlLoRa/Security/handshake.py",
    "AlLoRa/Security/identity.py",
    "AlLoRa/Security/Session.py",
    "AlLoRa/Security/Session_store.py",
    "AlLoRa/Security/Replay_window.py",
    "AlLoRa/Codec.py",
    "AlLoRa/Status.py",
    # The control path: the gate an Edge verifies with, the layout both ends share, and the
    # minting half, which the manifest freezes onto every node including ones that never sign.
    "AlLoRa/DataSinks/Control_Root_DataSink.py",
    "AlLoRa/Control/control_envelope.py",
    "AlLoRa/Control/Control_Root.py",
    "AlLoRa/Control/Control_Actuator.py",
    "AlLoRa/Nodes/Node.py",
    "AlLoRa/Nodes/Edge.py",
    "AlLoRa/Nodes/Hub.py",
    "AlLoRa/Digital_Endpoint.py",
    # The MQTT bridge halves are frozen and reached on-device too (a bridge Edge runs
    # the datasource + sink pair against its local broker).
    "AlLoRa/DataSources/DataSource.py",
    "AlLoRa/DataSources/Disk_DataSource.py",
    "AlLoRa/DataSources/MQTT_DataSource.py",
    "AlLoRa/DataSources/mqtt_naming.py",
    "AlLoRa/DataSources/Loop_guard.py",
    "AlLoRa/DataSinks/DataSink.py",
    "AlLoRa/DataSinks/MQTT_DataSink.py",
    # The all-or-nothing write itself, now on the send path too: a queued payload is
    # committed through it before the node will serve the file.
    "AlLoRa/utils/file_utils.py",
]

# Names CPython provides but MicroPython v1.24.1 does not (each one broke secure mode on-device).
# `import ucryptolib` is intentionally NOT here: cryptolib-first with a ucryptolib fallback is the
# correct portable form, so the name legitimately appears in code.
_CPYTHON_ONLY = [
    "bit_length",       # int.bit_length() — absent on MicroPython
    "digest_size",      # hashlib hash objects don't expose it
    "block_size",       # ditto
    "compare_digest",   # hmac.compare_digest is CPython-only
    "removeprefix",     # str/bytes 3.9+
    "removesuffix",
    "bit_count",        # int.bit_count() 3.10+
]

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_names(path):
    """Return the set of NAME/attribute tokens in a file, with comments and string/doc literals
    excluded — so `x.bit_length()` is seen but a comment mentioning it is not."""
    names = set()
    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type == tokenize.NAME:
                names.add(tok.string)
    return names


def test_secure_path_has_no_cpython_only_apis():
    offenders = []
    for rel in _ON_DEVICE_SECURE:
        names = _code_names(os.path.join(_REPO, rel))
        for bad in _CPYTHON_ONLY:
            if bad in names:
                offenders.append("{} uses {}".format(rel, bad))
    assert not offenders, "CPython-only API on the on-device secure path:\n  " + "\n  ".join(offenders)


def test_the_lint_actually_catches_a_violation(tmp_path):
    # Guard the guard: prove the scan flags a banned call and ignores it in a comment/string.
    good = tmp_path / "good.py"
    good.write_text("# mentions bit_length in a comment\nx = 'bit_length in a string'\n")
    bad = tmp_path / "bad.py"
    bad.write_text("n = (5).bit_length()\n")
    assert "bit_length" not in _code_names(str(good))
    assert "bit_length" in _code_names(str(bad))

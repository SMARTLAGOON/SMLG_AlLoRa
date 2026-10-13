"""Every library file compiles with MicroPython's own compiler, the version the firmware is built on.

The host suite runs on CPython, which accepts syntax MicroPython rejects, and the firmware build
only runs when firmware/ changes. Install the matching compiler with `pip install mpy-cross==<v>.*`.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "build-firmware.yml")


def _firmware_version():
    with open(WORKFLOW) as f:
        refs = set(re.findall(r"micropython_ref:\s*v([\d.]+)", f.read()))
    assert len(refs) == 1, "expected one MicroPython version in build-firmware.yml, found %s" % refs
    return refs.pop()


def _compiler():
    """The mpy-cross on PATH, or a skip that says which one to install."""
    want = _firmware_version()
    path = shutil.which("mpy-cross")
    if path is None:
        pytest.skip("mpy-cross is not installed: pip install mpy-cross==%s.*" % want)
    out = subprocess.run([path, "--version"], capture_output=True, text=True).stdout
    if "v%s " % want not in out:
        # A newer compiler accepts syntax the firmware's rejects, so a pass would mean nothing.
        pytest.skip("mpy-cross is not the firmware's MicroPython %s (%s): pip install mpy-cross==%s.*"
                    % (want, out.strip(), want))
    return path


def _library_files():
    for folder, dirs, files in os.walk(os.path.join(ROOT, "AlLoRa")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(folder, name)


def test_every_library_file_compiles_for_micropython(tmp_path):
    compiler = _compiler()
    failed = []
    for path in _library_files():
        result = subprocess.run([compiler, "-o", str(tmp_path / "out.mpy"), path],
                                capture_output=True, text=True)
        if result.returncode != 0:
            failed.append("%s\n%s" % (os.path.relpath(path, ROOT), result.stderr.strip()))
    assert not failed, "\n\n".join(failed)

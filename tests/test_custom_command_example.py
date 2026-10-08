"""`examples/custom_command/run_on_host.py` runs on CPython, as its README says.

The example is where a student starts adding a command of their own, so it has to keep working:
a Hub sends one signed CUSTOM command to each of two Edges over a simulated link, and each
Edge's actuator hands it to what runs beside it, one over a serial wire and one into a file a
program reads.
"""
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "examples", "custom_command", "run_on_host.py")


def test_the_custom_command_example_runs_and_both_edges_hand_it_over(tmp_path):
    done = subprocess.run([sys.executable, _SCRIPT, str(tmp_path)], cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=120)

    assert done.returncode == 0, done.stdout + done.stderr
    assert "serial device got: take a photo" in done.stdout
    assert "program restarted, it reads: new detection model" in done.stdout
    with open(os.path.join(str(tmp_path), "camera", "model.bin"), "rb") as f:
        assert f.read() == b"new detection model", "the program's file holds the payload as sent"

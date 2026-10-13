"""Shared by the tests that talk to the library as tagged v2.0.0: extract it, and run one of
its nodes in a child process (`v2_tag_peer.py`) whose frames are relayed in and out here.
"""
import os
import struct
import subprocess
import sys
import tarfile
import threading

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PEER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v2_tag_peer.py")


def extract_v2_tree(dest):
    """Extract the `AlLoRa` package exactly as tagged v2.0.0 into `dest`; return its path."""
    archive = dest / "v2.tar"
    done = subprocess.run(["git", "archive", "-o", str(archive), "v2.0.0", "AlLoRa"],
                          cwd=_REPO, capture_output=True, text=True)
    assert done.returncode == 0, (
        "the v2.0.0 tag is needed to check v2 compatibility; fetch it with "
        "`git fetch --tags` (CI checks out with fetch-depth 0): " + done.stderr)
    with tarfile.open(str(archive)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(str(dest), filter="data")
        else:
            tar.extractall(str(dest))
    return str(dest)


class V2Peer:
    """A v2.0.0 node in a child process, its frames relayed into `deliver` and logged."""

    def __init__(self, tmp_path, v2_tree, role, config, own_mac, arg, log, deliver):
        self.stderr_path = tmp_path / (role + "-" + own_mac + ".stderr")
        self._stderr = open(str(self.stderr_path), "wb")
        self.proc = subprocess.Popen([sys.executable, _PEER, v2_tree, role, config, own_mac, arg],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._stderr)
        self.role = role
        self.ready = threading.Event()
        self._relay = threading.Thread(target=self._run, args=(log, deliver), daemon=True)
        self._relay.start()
        assert self.ready.wait(timeout=30), self.explain("never started listening")

    def _run(self, log, deliver):
        while True:
            size = self.proc.stdout.read(2)
            if len(size) < 2:
                return
            n = struct.unpack("!H", size)[0]
            if n == 0:
                self.ready.set()
                continue
            wire = self.proc.stdout.read(n)
            log.append((self.role, wire))
            deliver(wire)

    def send(self, wire):
        try:
            self.proc.stdin.write(struct.pack("!H", len(wire)) + wire)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass    # the child finished; a radio would just not be heard

    def finish(self, timeout):
        """Wait for the child to exit and its last frame to be logged; return its exit code."""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = None
        self._relay.join(timeout=5)
        return code

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self._stderr.close()

    def explain(self, what):
        self._stderr.flush()
        tail = self.stderr_path.read_text(errors="replace")[-2000:]
        return "the v2.0.0 {} {} (exit code {}); its stderr ends:\n{}".format(
            self.role, what, self.proc.poll(), tail)

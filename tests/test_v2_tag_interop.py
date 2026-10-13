"""A v3 Hub set to `protocol_version: 2` against the library as tagged v2.0.0, not a copy of it.

The v2.0.0 code is taken from the git tag and run in a child process (`v2_tag_peer.py`), with a
pipe for a radio. Two promises are checked: the Hub pulls a whole file from an Edge running
v2.0.0, and every frame it sends on the way is the frame a v2.0.0 Hub sends at that point.
"""
import json
import os
import queue
import struct
import subprocess
import sys
import tarfile
import threading

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Hub import Hub

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PEER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v2_tag_peer.py")
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
# 1000 bytes at chunk_size 243 is five chunks, the last one short.
PAYLOAD = bytes(i % 256 for i in range(1000))


@pytest.fixture(scope="module")
def v2_tree(tmp_path_factory):
    """The `AlLoRa` package exactly as tagged v2.0.0, extracted from git."""
    dest = tmp_path_factory.mktemp("v2.0.0")
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


def _config(tmp_path, name, short_mac):
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps({
        "name": name,
        "protocol_version": 2,    # read by v3 only; v2.0.0 ignores the key
        "chunk_size": 243,
        "mesh_mode": False,
        "short_mac": short_mac,
        "debug": False,
        "result_path": str(tmp_path / (name + "-Results")),
        # A pipe loses nothing, so the timeout only has to outlast a loaded machine. A resend
        # is answered twice, and the spare reply would make the two runs' logs differ.
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 3, "debug": False},
    }))
    return str(path)


class _Peer:
    """A v2.0.0 node in a child process, its frames relayed into `deliver` and logged."""

    def __init__(self, tmp_path, v2_tree, role, config, own_mac, arg, log, deliver):
        self.stderr_path = tmp_path / (role + ".stderr")
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


def _v3_hub_against_v2_edge(tmp_path, v2_tree, short_mac):
    """Return (frames in order, the file the v3 Hub saved or None)."""
    log, inbox, outbox = [], queue.Queue(), queue.Queue()
    served = tmp_path / "served.bin"
    served.write_bytes(PAYLOAD)
    edge = _Peer(tmp_path, v2_tree, "edge", _config(tmp_path, "edge", short_mac), EDGE_MAC,
                 str(served), log, inbox.put)

    def hub_out():
        while True:
            wire = outbox.get()
            if wire is None:
                return
            log.append(("hub", wire))
            edge.send(wire)
    sender = threading.Thread(target=hub_out, daemon=True)
    sender.start()

    hub = Hub(Loopback_connector(HUB_MAC, inbox=inbox, outbox=outbox),
              config_file=_config(tmp_path, "hub", short_mac))
    try:
        hub.listen_to_endpoint(Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True),
                               listening_time=30, save_file=True, one_file=True)
        assert edge.finish(timeout=30) == 0, edge.explain("did not finish serving the file")
    finally:
        outbox.put(None)
        sender.join(timeout=5)
        edge.kill()
    saved = tmp_path / "hub-Results" / EDGE_MAC / "served.bin"
    return log, (saved.read_bytes() if saved.exists() else None)


def _v2_hub_against_v2_edge(tmp_path, v2_tree, short_mac):
    """The same exchange with both ends on v2.0.0: the reference the v3 Hub is held to."""
    log, peers = [], {}
    hub_known = threading.Event()
    served = tmp_path / "served.bin"
    served.write_bytes(PAYLOAD)

    def to_hub(wire):
        # The Edge can answer the Hub's first request before the Hub's _Peer has returned.
        hub_known.wait(timeout=30)
        peers["hub"].send(wire)

    peers["edge"] = _Peer(tmp_path, v2_tree, "edge", _config(tmp_path, "edge", short_mac),
                          EDGE_MAC, str(served), log, to_hub)
    try:
        peers["hub"] = _Peer(tmp_path, v2_tree, "hub", _config(tmp_path, "hub", short_mac),
                             HUB_MAC, EDGE_MAC, log, peers["edge"].send)
        hub_known.set()
        assert peers["edge"].finish(timeout=30) == 0, \
            peers["edge"].explain("did not finish the reference transfer")
        assert peers["hub"].finish(timeout=30) == 0, \
            peers["hub"].explain("failed in the reference transfer")
    finally:
        for peer in peers.values():
            peer.kill()
    return log


@pytest.mark.parametrize("short_mac", [True, False], ids=["short-mac", "long-mac"])
def test_a_v3_hub_in_v2_mode_pulls_a_file_from_v2_0_0_with_v2_0_0_frames(tmp_path, v2_tree,
                                                                          short_mac):
    (tmp_path / "v3").mkdir()
    (tmp_path / "v2").mkdir()
    v3_log, saved = _v3_hub_against_v2_edge(tmp_path / "v3", v2_tree, short_mac)
    reference = _v2_hub_against_v2_edge(tmp_path / "v2", v2_tree, short_mac)

    assert saved == PAYLOAD
    assert [w for d, w in v3_log if d == "hub"] == [w for d, w in reference if d == "hub"]
    # The Edge is the same v2.0.0 code in both runs, so equal replies mean it was asked the same
    # things in the same order.
    assert v3_log == reference

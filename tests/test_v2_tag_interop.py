"""A v3 Hub set to `protocol_version: 2` against the library as tagged v2.0.0, not a copy of it.

The v2.0.0 code is taken from the git tag and run in a child process (`v2_tag_peer.py`), with a
pipe for a radio. Two promises are checked: the Hub pulls a whole file from an Edge running
v2.0.0, and every frame it sends on the way is the frame a v2.0.0 Hub sends at that point.
"""
import json
import queue
import threading

import pytest

from v2_tag import V2Peer, extract_v2_tree
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Hub import Hub

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
# 1000 bytes at chunk_size 243 is five chunks, the last one short.
PAYLOAD = bytes(i % 256 for i in range(1000))


@pytest.fixture(scope="module")
def v2_tree(tmp_path_factory):
    return extract_v2_tree(tmp_path_factory.mktemp("v2.0.0"))


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


def _v3_hub_against_v2_edge(tmp_path, v2_tree, short_mac):
    """Return (frames in order, the file the v3 Hub saved or None)."""
    log, inbox, outbox = [], queue.Queue(), queue.Queue()
    served = tmp_path / "served.bin"
    served.write_bytes(PAYLOAD)
    edge = V2Peer(tmp_path, v2_tree, "edge", _config(tmp_path, "edge", short_mac), EDGE_MAC,
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
        # The Edge can answer the Hub's first request before the Hub's V2Peer has returned.
        hub_known.wait(timeout=30)
        peers["hub"].send(wire)

    peers["edge"] = V2Peer(tmp_path, v2_tree, "edge", _config(tmp_path, "edge", short_mac),
                           EDGE_MAC, str(served), log, to_hub)
    try:
        peers["hub"] = V2Peer(tmp_path, v2_tree, "hub", _config(tmp_path, "hub", short_mac),
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

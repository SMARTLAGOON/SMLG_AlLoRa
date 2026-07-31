"""The payload ceiling: how many bytes of a file fit in one frame.

`calculate_max_chunk_size` clamps a node's configured `chunk_size` to what the radio can
actually carry beside the framing. It used to subtract the *v2* header unconditionally, so a
v3 node was held at v2's ceiling (243 at SF7, 18 at SF12) no matter which codec it spoke.
That made session-id addressing's whole payload gain unreachable, and would have hidden the
secure header's width entirely. The overhead now comes from the codec in use.

Ceilings, at BW125: SF7 to SF10 carry 255 PHY bytes, SF11 111, SF12 30.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge

MAC = "a1a1a1a1"


def _config(path, chunk_size, protocol_version=3, security_mode="open", sf=7,
            mesh_mode=False):
    config = {
        "name": "ceiling", "chunk_size": chunk_size, "mesh_mode": mesh_mode,
        "short_mac": True, "protocol_version": protocol_version,
        "security_mode": security_mode, "session_id": 42, "debug": False,
        "connector": {"sf": sf, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def _node(tmp_path, name="LoRa.json", **kwargs):
    path = _config(str(tmp_path / name), **kwargs)
    return Edge(Loopback_connector(MAC), config_file=path)


def test_v2_keeps_its_two_mac_ceiling(tmp_path):
    # 255 - 12. Unchanged: v2 really does spend 12 bytes on two MAC addresses.
    node = _node(tmp_path, chunk_size=255, protocol_version=2)
    assert node.chunk_size == 243


def test_v3_open_reaches_past_the_v2_ceiling(tmp_path):
    # 255 - 6. This is the +6 B/chunk that session-id addressing bought, and it was
    # previously unreachable: the clamp cut it back to v2's 243.
    node = _node(tmp_path, chunk_size=255)
    assert node.chunk_size == 249


def test_v3_open_no_longer_clamps_a_configuration_it_can_carry(tmp_path):
    # The regression that mattered in the field: asking for 249 silently got you 243.
    node = _node(tmp_path, chunk_size=249)
    assert node.chunk_size == 249


def test_v3_secure_ceiling_leaves_room_for_the_sealed_header_and_tag(tmp_path):
    # 255 - (4 + 4). Lower than open, because the tag replaces the integrity trailer and
    # the counter has to travel, but still above v2.
    node = _node(tmp_path, chunk_size=255, security_mode="secure")
    assert node.chunk_size == 247


def test_the_ceiling_follows_the_radio_down_to_sf12(tmp_path):
    # SF12 carries 30 bytes total, so the header is a third of the frame and the difference
    # between postures is proportionally huge: v2 got 18 where v3 open gets 24.
    assert _node(tmp_path, "v3.json", chunk_size=255, sf=12).chunk_size == 24
    assert _node(tmp_path, "v2.json", chunk_size=255, sf=12,
                 protocol_version=2).chunk_size == 18


def test_a_configuration_below_the_ceiling_is_left_alone(tmp_path):
    node = _node(tmp_path, chunk_size=100)
    assert node.chunk_size == 100

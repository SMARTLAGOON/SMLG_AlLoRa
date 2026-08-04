"""The payload ceiling is the PHY's, and the receive window is sized for real traffic.

`get_max_payload_size()` used to return 111 at SF11 and 30 at SF12. Those numbers were tuned
in October 2024 on hardware that never had LowDataRateOptimize set, where long frames at high
spreading factors really did fail; the surviving `#51` comment beside the 30 shows the value
being walked down until it worked. With the register written, a raw sweep put 18 of 18 frames
intact at 255 bytes at SF12. The table was describing a fault, not the PHY, so v3 now reports
the PHY: 255 bytes at every spreading factor and bandwidth.

v2 keeps the old table on purpose. It is frozen, it shipped with those numbers, and it is the
baseline the v3 throughput comparison is measured against: changing what a v2 node does would
rewrite the instrument rather than the protocol.

Raising the ceiling drags the receive window with it, because the window floor was sized from
the time on air of a maximum-size frame. At SF12 that would take a node sending 30-byte chunks
from a 1.5 s floor to a 7.7 s one, for frames it never sends. The floor is therefore sized
from the largest frame the node will actually transmit.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Nodes.Edge import Edge

MAC = "a1a1a1a1"
TIMEOUT_DELTA = 0.1


def _config(path, chunk_size, protocol_version=3, security_mode="open", sf=7, bandwidth=125):
    config = {
        "name": "ceiling", "chunk_size": chunk_size, "mesh_mode": False,
        "short_mac": True, "protocol_version": protocol_version,
        "security_mode": security_mode, "session_id": 42, "debug": False,
        "connector": {"sf": sf, "freq": 868, "bandwidth": bandwidth, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": TIMEOUT_DELTA, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def _node(tmp_path, name="LoRa.json", **kwargs):
    path = _config(str(tmp_path / name), **kwargs)
    return Edge(Loopback_connector(MAC), config_file=path)


# --- D2: the ceiling ----------------------------------------------------------------------

@pytest.mark.parametrize("sf", [7, 10, 11, 12])
@pytest.mark.parametrize("bandwidth", [125, 250, 500])
def test_v3_reports_the_phy_ceiling_at_every_sf_and_bandwidth(tmp_path, sf, bandwidth):
    node = _node(tmp_path, chunk_size=255, sf=sf, bandwidth=bandwidth)

    assert node.connector.get_max_payload_size() == 255


def test_sf12_is_no_longer_held_at_a_thirtieth_of_the_frame(tmp_path):
    # Was 24: a 30-byte ceiling minus the 6-byte v3 open header. The single biggest effect of
    # this change, and the one the LDRO bench unblocked.
    node = _node(tmp_path, chunk_size=255, sf=12)

    assert node.chunk_size == 249


def test_sf11_reaches_the_same_ceiling_as_sf7(tmp_path):
    assert _node(tmp_path, "a.json", chunk_size=255, sf=11).chunk_size == 249
    assert _node(tmp_path, "b.json", chunk_size=255, sf=7).chunk_size == 249


@pytest.mark.parametrize("sf, expected", [(7, 243), (11, 99), (12, 18)])
def test_v2_keeps_the_table_it_shipped_with(tmp_path, sf, expected):
    # The frozen baseline. If these move, the v2 arm of every throughput comparison moves
    # with them and stops being a measurement of v2.
    node = _node(tmp_path, chunk_size=255, sf=sf, protocol_version=2)

    assert node.chunk_size == expected


def test_a_configuration_below_the_ceiling_is_still_left_alone(tmp_path):
    assert _node(tmp_path, chunk_size=100, sf=12).chunk_size == 100


# --- D3: the receive-window floor ---------------------------------------------------------

def _expected_floor(connector, frame_size):
    return connector.calculate_toa(connector.sf, connector.bw, connector.cr,
                                   frame_size) + TIMEOUT_DELTA


def test_the_floor_is_sized_for_the_frame_the_node_actually_sends(tmp_path):
    # A 30-byte chunk at SF12 is a 36-byte frame. Sizing the window from the 255-byte ceiling
    # instead would make this node wait roughly five times as long for every reply.
    node = _node(tmp_path, chunk_size=30, sf=12)
    connector = node.connector

    assert connector.min_timeout == pytest.approx(_expected_floor(connector, 36))


def test_a_full_size_node_gets_a_window_that_fits_its_frames(tmp_path):
    node = _node(tmp_path, chunk_size=255, sf=12)
    connector = node.connector

    assert node.chunk_size == 249
    assert connector.min_timeout == pytest.approx(_expected_floor(connector, 255))


def test_raising_the_ceiling_did_not_inflate_a_small_chunk_node(tmp_path):
    # The regression D3 exists to prevent, stated as the comparison that matters: an SF12
    # node sending small chunks must not wait as though it sent big ones.
    small = _node(tmp_path, "small.json", chunk_size=30, sf=12)
    large = _node(tmp_path, "large.json", chunk_size=255, sf=12)

    assert small.connector.min_timeout < large.connector.min_timeout / 4


def test_the_floor_follows_a_chunk_size_change(tmp_path):
    node = _node(tmp_path, chunk_size=30, sf=12)
    before = node.connector.min_timeout

    node.change_rf_config({"cks": 249})

    assert node.chunk_size == 249
    assert node.connector.min_timeout > before
    assert node.connector.min_timeout == pytest.approx(_expected_floor(node.connector, 255))


def test_the_floor_follows_a_spreading_factor_change(tmp_path):
    node = _node(tmp_path, chunk_size=30, sf=7)
    before = node.connector.min_timeout

    node.change_rf_config({"sf": 12})

    assert node.connector.min_timeout > before
    assert node.connector.min_timeout == pytest.approx(_expected_floor(node.connector, 36))


def test_the_ceiling_still_covers_a_frame_bigger_than_this_node_sends(tmp_path):
    # The safety property, and the reason the floor and the ceiling are sized from different
    # numbers. Pacing can never grow its window past max_timeout, so a node whose peer sends
    # fuller frames than it does must still have a ceiling that fits one, or it can never
    # receive at all. A small-chunk node keeps a small floor and a full-size ceiling.
    node = _node(tmp_path, chunk_size=30, sf=12)
    connector = node.connector
    biggest_frame_it_could_receive = connector.calculate_toa(
        connector.sf, connector.bw, connector.cr, 255)

    assert connector.max_timeout > biggest_frame_it_could_receive
    assert connector.min_timeout < biggest_frame_it_could_receive


def test_the_ceiling_is_unchanged_for_a_node_that_fills_its_frames(tmp_path):
    # No regression for the full-size case: floor and ceiling coincide as they always did.
    node = _node(tmp_path, chunk_size=255, sf=12)
    connector = node.connector

    assert connector.max_timeout == pytest.approx(
        2 * connector.calculate_toa(connector.sf, connector.bw, connector.cr, 255)
        + TIMEOUT_DELTA)


def test_a_bare_connector_falls_back_to_the_ceiling(tmp_path):
    # No node has said how big its frames are, so the PHY ceiling stands in. This is the
    # path config() takes before any node exists, and it must not divide by a missing hint.
    connector = Loopback_connector(MAC)
    connector.config({"sf": 12, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": TIMEOUT_DELTA, "debug": False,
                      "protocol_version": 3})

    assert connector.min_timeout == pytest.approx(_expected_floor(connector, 255))

"""A failed RF-config change must mutate nothing.

The connector layer is already all-or-nothing: `Connector.change_rf_config` snapshots the
radio, applies the new parameters, and on any exception restores the whole backup and
returns False. The node above it was not. It applied the new `chunk_size` and published the
new frame size *before* it tested whether the radio had accepted anything, so a change the
radio rejected and rolled back still left the node carrying a chunk size from the config
that failed.

The concrete harm is in the serve path, which compares `chunk_size` against a pre-call
backup and re-chunks the in-flight file when the two differ: a *failed* RF change re-chunked
a transfer that was proceeding fine. A rejected change must leave the node byte-for-byte as
it was.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Packet import Packet

EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"
UNSUPPORTED_SF = 99      # no radio in the family accepts this one


class Rejecting_loopback(Loopback_connector):
    """A loopback whose radio refuses an unsupported spreading factor, the way a driver
    does: the setter raises, and the connector's own rollback restores every parameter and
    returns False. Nothing here stubs that rollback out, so the test drives the real one.
    """

    def set_sf(self, sf):
        if sf == UNSUPPORTED_SF:
            raise ValueError("SF {} is not supported by this radio".format(sf))
        super().set_sf(sf)


def _edge(tmp_path, chunk_size=100, sf=7):
    config = {
        "name": "atomicity", "chunk_size": chunk_size, "mesh_mode": False,
        "short_mac": True, "protocol_version": 3, "security_mode": "open",
        "session_id": 42, "debug": False,
        "connector": {"sf": sf, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    path = str(tmp_path / "LoRa.json")
    with open(path, "w") as f:
        json.dump(config, f)
    return Edge(Rejecting_loopback(EDGE_MAC), config_file=path)


def test_a_rejected_change_leaves_the_chunk_size_alone(tmp_path):
    # The radio refuses the spreading factor, so the whole change is off: the chunk size
    # that rode along with it has no more claim on the node than the SF did.
    edge = _edge(tmp_path, chunk_size=100)

    accepted = edge.change_rf_config({"sf": UNSUPPORTED_SF, "cks": 200})

    assert accepted is False, "an RF change the radio refused is not an accepted change"
    assert edge.get_chunk_size() == 100, "a rejected change must not move the chunk size"


def test_a_rejected_change_leaves_the_receive_window_alone(tmp_path):
    # The other mutation that used to run before the result was tested. The frame size is
    # what the connector sizes its receive window from, so publishing one for a config the
    # radio is not on leaves the node listening on the wrong window.
    edge = _edge(tmp_path, chunk_size=100)
    frame_size_before = edge.connector.frame_size

    edge.change_rf_config({"sf": UNSUPPORTED_SF, "cks": 200})

    assert edge.connector.frame_size == frame_size_before, (
        "a rejected change published a frame size for a config the radio never took")


def _v2_edge_serving(tmp_path, chunk_size=100, content_bytes=500):
    # A legacy node (no protocol_version in the config) part-way through an uplink. v2 is
    # where the in-band RF-change verb is live today, so it is where the serve path can be
    # driven end to end.
    config = {
        "name": "atomicity-v2", "chunk_size": chunk_size, "mesh_mode": False,
        "short_mac": True, "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    path = str(tmp_path / "LoRa_v2.json")
    with open(path, "w") as f:
        json.dump(config, f)
    edge = Edge(Rejecting_loopback(EDGE_MAC), config_file=path)
    edge.set_file(AlLoRa_File(name="up.bin", content=bytearray(bytes(content_bytes)),
                              chunk_size=edge.get_chunk_size()))
    return edge


def _rf_change_request(new_config):
    request = Packet(mesh_mode=False, short_mac=True)
    request.set_source(HUB_MAC)
    request.set_destination(EDGE_MAC)
    request.set_change_rf(new_config)
    return request


def test_a_rejected_change_does_not_re_chunk_a_transfer_in_flight(tmp_path):
    # The harm the atomicity rule closes. The serve path re-chunks the file it is serving
    # whenever the chunk size moved across the change, so a chunk size that leaked out of a
    # *rejected* change re-cut a transfer that was proceeding fine: the receiver goes on
    # asking for the chunks it was promised and gets different bytes back.
    edge = _v2_edge_serving(tmp_path, chunk_size=100, content_bytes=500)
    edge.file.get_chunk(0)                  # the transfer is under way
    chunks_before = edge.file.get_length()
    assert chunks_before == 5

    edge.connector.inbox.put(_rf_change_request(
        {"sf": UNSUPPORTED_SF, "cks": 200}).get_content())
    edge.serve(timeout=1)

    assert edge.get_chunk_size() == 100, "the node kept a chunk size the radio refused"
    assert edge.file.get_length() == chunks_before, (
        "a rejected RF change re-cut the file in flight: the receiver's chunk indices no "
        "longer address the bytes it was promised")

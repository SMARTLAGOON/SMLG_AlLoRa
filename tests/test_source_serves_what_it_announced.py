"""A source serves only what it remembers announcing.

The splice found on hardware on 2026-08-10 needs two ends that disagree about whether a
transfer is open. A source that reboots mid-file comes back holding whatever its queue
hands it next; the collector, which drives, keeps asking for the next index and gets
answers from a file it never asked for. The length check shipped the day before catches
the case where the two files differ in size, and is blind when they match, which in a
deployment writing fixed-format records is the common case.

What the reboot actually destroyed is the source's own memory of having announced the
file. `metadata_sent` already holds that memory, so the source has the fact it needs.
These tests pin it to using it: a chunk request for a file it does not remember
announcing is answered with METADATA, which says what it is serving now, and never with
data. Answering rather than going quiet is deliberate: a collector reads silence as a
lost frame and re-asks the same index forever.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes.Edge import Edge
from AlLoRa.Packet_v3 import Packet_v3

SESSION_ID = 42
SOURCE_MAC = "a1a1a1a1"
COLLECTOR_MAC = "b2b2b2b2"
CHUNK_SIZE = 243


def _write_config(path):
    config = {
        "name": "announcing",
        "chunk_size": CHUNK_SIZE,
        "mesh_mode": False,
        "protocol_version": 3,
        "security_mode": "open",
        "session_id": SESSION_ID,
        "debug": False,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_source(tmp_path, datasource=None):
    config_path = str(tmp_path / "edge.json")
    _write_config(config_path)
    conn, _ = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    return Edge(conn, config_file=config_path, datasource=datasource)


def _file(name, payload):
    return AlLoRa_File(name=name, content=bytearray(payload), chunk_size=CHUNK_SIZE)


def _chunk_request(index):
    packet = Packet_v3(addressing="sid")
    packet.set_session(SESSION_ID)
    packet.ask_data(index)
    return packet


def _metadata_request():
    packet = Packet_v3(addressing="sid")
    packet.set_session(SESSION_ID)
    packet.ask_metadata()
    return packet


PAYLOAD = bytes(i % 256 for i in range(600))    # 3 chunks at 243


def test_a_chunk_request_for_an_unannounced_file_is_answered_with_metadata(tmp_path):
    source = _make_source(tmp_path)
    # What a reboot leaves behind: the file is installed fresh off the queue, so nothing
    # on it records an announcement. The collector is mid-transfer and asks for index 2.
    source.set_file(_file("hello.bin", PAYLOAD))

    reply, _ = source.response(_chunk_request(2))

    assert reply is not None, "silence stalls the collector; 0015 forbids it as a refusal"
    assert reply.get_command() == Packet_v3.METADATA
    assert reply.get_metadata()["FILENAME"] == "hello.bin"


def test_the_re_announcement_carries_what_the_source_holds_now(tmp_path):
    source = _make_source(tmp_path)
    source.set_file(_file("hello.bin", PAYLOAD))

    reply, _ = source.response(_chunk_request(2))
    metadata = reply.get_metadata()

    # The whole point of answering with METADATA rather than going quiet: it is the only
    # exchange that says which file this is and how long it is.
    assert metadata["CHUNK_SIZE"] == CHUNK_SIZE
    assert metadata["TOTAL_LEN"] == len(PAYLOAD)
    assert metadata["LENGTH"] == 3


def test_a_source_that_announced_the_file_serves_the_chunk(tmp_path):
    source = _make_source(tmp_path)
    source.set_file(_file("hello.bin", PAYLOAD))

    source.response(_metadata_request())            # the announcement it remembers
    reply, _ = source.response(_chunk_request(0))

    assert reply.get_command() == Packet_v3.DATA
    assert reply.get_payload() == PAYLOAD[:CHUNK_SIZE]


def test_a_lost_re_announcement_does_not_let_the_next_request_through(tmp_path):
    """The re-announcement must not count as the announcement.

    Nothing acknowledges it, so a lost one leaves the collector re-asking the same index.
    If answering had marked the file announced, that repeat would be served from the new
    file and splice exactly what this prevents. Repeating the METADATA instead is
    self-healing: the collector re-opens whenever one of them lands.
    """
    source = _make_source(tmp_path)
    source.set_file(_file("hello.bin", PAYLOAD))

    for _ in range(3):
        reply, _ = source.response(_chunk_request(2))
        assert reply.get_command() == Packet_v3.METADATA


# --- resume across a restart is the input boundary's to permit ----------------------------
#
# D1 on its own would make every restart a restart of the transfer, including the one case
# where continuing is both safe and valuable: a queue that kept the file on flash hands back
# the same bytes under the same name, so an hour of a long transfer need not be re-sent. The
# source cannot tell that case from a swapped file, and should not try. The queue can,
# because it owns the storage, so it is asked.


def test_a_ram_queue_restarts_the_transfer_after_a_restart(tmp_path):
    # The base queue lived in RAM and lost the file, so whatever it hands back now is a
    # different file wearing the position: announce it and let the collector start over.
    source = _make_source(tmp_path, datasource=DataSource(file_chunk_size=CHUNK_SIZE))
    source.datasource.add_to_queue(_file("hello.bin", PAYLOAD))

    source._pump_datasource()
    reply, _ = source.response(_chunk_request(2))

    assert reply.get_command() == Packet_v3.METADATA


def _durable_queue(tmp_path):
    queue = Disk_DataSource(file_chunk_size=CHUNK_SIZE,
                            queue_path=str(tmp_path / "Outbox"))
    queue.prepare()
    return queue


def test_a_durable_queue_resumes_the_transfer_after_a_restart(tmp_path):
    """The regression D3 exists to prevent, proved on radio on 2026-08-09.

    A queue on flash still holds the unconfirmed file and still has it at the head, so a
    transfer the reboot interrupted carries on from the collector's next missing index.
    The source never has to know which chunk was last: the collector drives and asks for
    its own gaps. Re-announcing would throw away every chunk already sent, and on a long
    file over a slow link that is an hour of airtime.
    """
    queue = _durable_queue(tmp_path)
    queue.enqueue("hello.bin", PAYLOAD)
    source = _make_source(tmp_path, datasource=queue)
    source._pump_datasource()
    source.response(_metadata_request())
    source.response(_chunk_request(0))
    source.response(_chunk_request(1))

    # The board resets. Nothing in RAM survives it, so both the node and its queue are
    # built again from what is on flash.
    rebooted = _make_source(tmp_path, datasource=_durable_queue(tmp_path))
    rebooted._pump_datasource()
    reply, _ = rebooted.response(_chunk_request(2))

    assert reply.get_command() == Packet_v3.DATA, (
        "a queue that still holds the file must continue the transfer, not restart it")
    assert reply.get_payload() == PAYLOAD[2 * CHUNK_SIZE:]


def _make_v2_source(tmp_path):
    config_path = str(tmp_path / "edge_v2.json")
    config = {
        "name": "announcing-v2",
        "chunk_size": CHUNK_SIZE,
        "mesh_mode": False,
        "short_mac": True,
        "debug": False,
        "connector": {
            "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
            "tx_power": 14, "timeout_delta": 0.1, "debug": False,
        },
    }
    with open(config_path, "w") as f:
        json.dump(config, f)
    conn, _ = Loopback_connector.create_pair(SOURCE_MAC, COLLECTOR_MAC)
    return Edge(conn, config_file=config_path)


def test_restoring_a_file_by_hand_is_v2_only(tmp_path):
    """restore_file installs a file as already announced on the caller's say-so.

    In v2 that say-so was earned a line earlier: establish_connection had just heard the
    peer ask for something other than a connection poll, which is the peer saying a
    transfer is already open. v3 retired that step, so the same call now asserts an
    announcement nobody made, and the chunks served on the back of it go into a
    reassembly they may not belong to. That is this whole defect, entered through the
    front door. In v3 the input boundary vouches instead, and only if it can.

    Refused at the call, where the mistake is, rather than deep in the serve loop, which
    is the same shape as establish_connection's own v3 guard.
    """
    source = _make_source(tmp_path)
    with pytest.raises(NotImplementedError):
        source.restore_file(_file("hello.bin", PAYLOAD))
    assert source.file is None, "a refused restore must not leave the file installed"


def test_a_v2_node_restores_a_file_as_already_announced(tmp_path):
    # Unchanged for the deployments it was written for: v2 proved the transfer was open
    # before calling this, so the file goes back in mid-flight and chunks flow again.
    source = _make_v2_source(tmp_path)

    source.restore_file(_file("hello.bin", PAYLOAD))

    assert source.file.metadata_sent is True
    assert source.file.first_sent is not None

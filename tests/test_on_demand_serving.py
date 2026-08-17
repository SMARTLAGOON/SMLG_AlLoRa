"""Serving a queued file straight off flash, instead of holding it all in RAM.

`peek_file` used to read the whole payload and hand it to an `AlLoRa_File` that kept it
too: two copies of the artifact resident for as long as the node was serving it. That put
a ceiling on what a node could send (well under the 1 MB the 2024 paper published), taxed
every radio round with a collection proportional to the file, and left the node deaf for
the whole of the load.

These pin the replacement: the queue hands over a positioned reader, the chunks that come
out are byte-for-byte what a RAM copy would have produced, and the handle that makes it
possible is closed on every path the file can leave by.

The proof that the reads are correct on the target runtime is not here: `slice.indices()`
and the whole positioned-read path were checked on both boards, and the run sheet is in
the cockpit under `docs/hw-artifacts/v3-slice-indices-gate/`.
"""
import json

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.File import AlLoRa_File, OnDemandFileReader
from AlLoRa.Nodes.Edge import Edge

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"

# Big enough that the last chunk is a short one at both sizes used below, and that a
# whole-file comparison would notice a single misplaced byte.
PAYLOAD = bytes((i * 7 + 3) % 256 for i in range(5000))


def _queue(tmp_path, name="outbox", **kwargs):
    ds = Disk_DataSource(queue_path=str(tmp_path / name), **kwargs)
    ds.prepare()
    return ds


def _make_edge(tmp_path, datasource=None, chunk_size=243):
    config_path = str(tmp_path / "edge.json")
    config = {
        "name": "queued",
        "chunk_size": chunk_size,
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
    with open(config_path, "w") as f:
        json.dump(config, f)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=config_path, datasource=datasource)


def _all_chunks(f):
    return b"".join(f.get_chunk(i) for i in range(f.get_length()))


# -- the file is no longer resident ---------------------------------------------------------


def test_a_peeked_file_is_backed_by_flash_and_not_by_a_ram_copy(tmp_path):
    # The change itself: the queue hands over a reader positioned on the file, not the
    # file's bytes. Structural on purpose, because "no copy in RAM" is the whole claim
    # and every behavioural test below would still pass with the bytes resident.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    assert isinstance(f.content, OnDemandFileReader)
    assert not isinstance(f.content, (bytes, bytearray))


def test_the_file_knows_its_byte_count_without_reading_it(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    assert ds.peek_file().length == len(PAYLOAD)


# -- the bytes that come out are the right bytes --------------------------------------------


@pytest.mark.parametrize("chunk_size", [249, 243, 200, 1, len(PAYLOAD), len(PAYLOAD) + 1])
def test_the_chunks_reconstruct_the_file_exactly(tmp_path, chunk_size):
    # Across sizes that divide the payload and sizes that leave a short tail, plus the
    # degenerate one-byte cut and a chunk larger than the whole file.
    ds = _queue(tmp_path, name="outbox-{}".format(chunk_size))
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    f.change_chunk_size(chunk_size)
    assert _all_chunks(f) == PAYLOAD


def test_the_last_chunk_is_the_short_one(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)          # 5000 bytes
    f = ds.peek_file()
    f.change_chunk_size(249)                    # 20 full chunks, then 20 bytes
    assert f.get_length() == 21
    assert len(f.get_chunk(20)) == 5000 - 20 * 249
    assert f.get_chunk(20) == PAYLOAD[20 * 249:]


def test_a_retransmitted_chunk_reads_the_same_bytes(tmp_path):
    # The collector asks for what it is missing, in whatever order it noticed. A reader
    # that only ever moved forward would answer the second request from the wrong offset.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    f.change_chunk_size(249)
    first = f.get_chunk(0)
    f.get_chunk(15)
    f.get_chunk(3)
    assert f.get_chunk(0) == first == PAYLOAD[:249]
    assert f.get_chunk(15) == PAYLOAD[15 * 249:16 * 249]


def test_a_chunk_past_the_end_is_empty_rather_than_an_error(tmp_path):
    # A peer asking beyond the file (a stale chunk count after a re-cut) must not raise
    # inside the serve loop.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    f.change_chunk_size(249)
    assert f.get_chunk(999) == b""


def test_re_cutting_after_a_retune_reads_the_new_size(tmp_path):
    # Change 3 made the node stamp its clamped size on every install, so a file whose
    # first attempt did not finish is re-cut when the radio has moved under it. The
    # flash-backed content has to survive being handed a size after construction.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    f.change_chunk_size(249)
    assert f.get_chunk(1) == PAYLOAD[249:498]
    f.change_chunk_size(100)
    assert f.get_length() == 50
    assert f.get_chunk(1) == PAYLOAD[100:200]
    assert _all_chunks(f) == PAYLOAD


# -- get_content still means "the bytes" ----------------------------------------------------


def test_get_content_still_returns_the_whole_payload(tmp_path):
    # Nothing in production calls this on a served file, but it is the one verb that
    # promises bytes, and a v2 subclass may. It reads the file rather than returning the
    # reader, so its type does not change under anyone.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    assert bytes(ds.peek_file().get_content()) == PAYLOAD


def test_get_content_does_not_leave_the_payload_resident(tmp_path):
    # Reading it once must not turn the file into the RAM copy this change removes.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    f = ds.peek_file()
    f.get_content()
    assert isinstance(f.content, OnDemandFileReader)


# -- the handle is closed on every exit ------------------------------------------------------


def test_the_handle_is_closed_when_the_peer_confirms(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    reader = ds.peek_file().content
    ds.confirm_file()
    assert reader.closed is True


def test_the_handle_is_closed_when_the_queue_is_closed(tmp_path):
    # Shutting a node down is not delivering its backlog, but it does mean letting go.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    reader = ds.peek_file().content
    ds.close()
    assert reader.closed is True


def test_the_handle_is_closed_when_the_head_is_dropped_as_unusable(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", PAYLOAD)
    ds.enqueue("b.bin", PAYLOAD)
    reader = ds.peek_file().content
    ds._forget("a.bin")
    assert reader.closed is True


def test_serving_many_files_leaves_one_handle_open_at_a_time(tmp_path):
    # The leak this creates if nothing closes: a node serves thousands of files between
    # reboots, and each peek would open one more.
    ds = _queue(tmp_path)
    readers = []
    for i in range(20):
        ds.enqueue("f{}.bin".format(i), PAYLOAD)
    while ds.has_pending():
        readers.append(ds.peek_file().content)
        ds.confirm_file()
    assert len(readers) == 20
    assert all(r.closed for r in readers)


def test_the_handle_is_closed_before_the_file_is_erased(tmp_path):
    # Order matters on an embedded filesystem: removing a file out from under an open
    # handle is not defined the way it is on POSIX.
    order = []

    class _Watched(Disk_DataSource):
        def _remove(self, name):
            order.append(("remove", name))
            return super()._remove(name)

    ds = _Watched(queue_path=str(tmp_path / "outbox"))
    ds.prepare()
    ds.enqueue("a.bin", PAYLOAD)
    reader = ds.peek_file().content

    real_close = reader.close

    def _watched_close():
        order.append(("close", "a.bin"))
        return real_close()

    reader.close = _watched_close
    ds.confirm_file()
    assert order == [("close", "a.bin"), ("remove", "a.bin")]


# -- the degenerate queue entries ------------------------------------------------------------


def test_an_empty_queued_file_is_dropped_rather_than_served(tmp_path):
    # A zero-length file has no chunks to ask for, so it would hold the front of the queue
    # forever. enqueue refuses one, but a file can also be dropped into the folder by hand,
    # and the reader path no longer reads the bytes that used to reveal it.
    ds = _queue(tmp_path)
    ds.enqueue("good.bin", PAYLOAD)
    with open(str(tmp_path / "outbox" / "empty.bin"), "wb"):
        pass
    ds._reconcile()
    names = []
    while ds.has_pending():
        f = ds.peek_file()
        if f is None:
            break
        names.append(f.get_name())
        ds.confirm_file()
    assert names == ["good.bin"]


def test_an_empty_queued_file_never_becomes_a_reassembly_file(tmp_path):
    # The specific hazard: AlLoRa_File branches on the truthiness of `content`, and an
    # empty reader is falsy, so an unguarded empty file would come back as a *receiving*
    # file, creating Temp folders on the source and answering get_length() with None.
    ds = _queue(tmp_path)
    with open(str(tmp_path / "outbox" / "empty.bin"), "wb"):
        pass
    ds._reconcile()
    assert ds.peek_file() is None


def test_an_unreadable_queued_file_is_dropped_and_the_next_is_tried(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("good.bin", PAYLOAD)
    ds._order.insert(0, "ghost.bin")        # in the index, never on the card
    assert ds.peek_file().get_name() == "good.bin"


# -- the legacy destructive pop --------------------------------------------------------------


def test_legacy_get_next_file_hands_back_readable_bytes_after_the_erase(tmp_path):
    # Its contract is "take the file and erase it", which only means anything if what
    # comes back can still be read. A reader positioned on a file that has just been
    # deleted cannot be, so this path materialises the payload before confirming.
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", PAYLOAD)
    f = ds.get_next_file()
    assert f.get_name() == "a.bin"
    assert not ds.has_pending()
    assert bytes(f.get_content()) == PAYLOAD


def test_legacy_get_next_file_returns_none_if_the_file_vanished_under_it(tmp_path):
    # There for the peek, gone for the read. Handing back an empty content instead would
    # come back as a *receiving* file: assembly_needed, a chunk count of None, and Temp
    # folders laid down on the sending side.
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", PAYLOAD)

    class _Vanishing(Disk_DataSource):
        def _read(self, name):
            return None

    ds.__class__ = _Vanishing
    assert ds.get_next_file() is None
    assert not ds.has_pending()


# -- through the node ------------------------------------------------------------------------


def test_the_node_serves_a_flash_backed_file_cut_to_its_own_size(tmp_path):
    # End to end through the install path change 3 settled: the queue states no chunk
    # size, the node stamps its clamped one, and the chunks come off flash.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    edge = _make_edge(tmp_path, datasource=ds, chunk_size=243)
    edge._pump_datasource()
    assert edge.file is not None
    assert edge.file.chunk_size == 243
    assert isinstance(edge.file.content, OnDemandFileReader)
    assert _all_chunks(edge.file) == PAYLOAD


def test_a_file_the_peer_never_confirmed_is_served_again_from_flash(tmp_path):
    # At-least-once: the attempt failed, the file stayed queued, and the second attempt
    # reads the same bytes rather than a stale handle's.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", PAYLOAD)
    edge = _make_edge(tmp_path, datasource=ds, chunk_size=243)
    edge._pump_datasource()
    assert _all_chunks(edge.file) == PAYLOAD
    edge._retire_file(delivered=False)

    edge._pump_datasource()
    assert edge.file is not None
    assert _all_chunks(edge.file) == PAYLOAD

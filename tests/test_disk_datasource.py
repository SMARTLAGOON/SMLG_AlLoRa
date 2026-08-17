"""Disk_DataSource: an outbound queue that keeps a file until the peer confirms it.

The RAM queue on the DataSource base loses whatever it is holding when the node loses
power, so a reading taken but not yet sent is gone with nothing to retry. These pin the
two halves of the fix: the queue itself lives on flash, and the node's serve loop borrows
its head rather than taking it, so the file is only erased once the far end acknowledged
the transfer.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes.Edge import Edge

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"


def _queue(tmp_path, name="outbox", **kwargs):
    ds = Disk_DataSource(queue_path=str(tmp_path / name), **kwargs)
    ds.prepare()
    return ds


def _make_edge(tmp_path, datasource=None):
    config_path = str(tmp_path / "edge.json")
    config = {
        "name": "queued",
        "chunk_size": 243,
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


# -- the queue on its own -------------------------------------------------------------------


def test_a_queued_file_comes_back_after_a_reboot(tmp_path):
    # The whole point: the node died holding this file, and the file is still there.
    ds = _queue(tmp_path)
    ds.enqueue("reading.bin", b"12345")
    assert ds.peek_file().get_name() == "reading.bin"

    rebooted = _queue(tmp_path)
    assert rebooted.has_pending()
    assert bytes(rebooted.peek_file().get_content()) == b"12345"


def test_peek_retains_the_head_until_confirm(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    assert ds.peek_file().get_name() == "a.bin"
    assert ds.peek_file().get_name() == "a.bin"
    assert ds.has_pending()
    assert ds.confirm_file().get_name() == "a.bin"
    assert not ds.has_pending()
    assert ds.peek_file() is None
    assert ds.confirm_file() is None


def test_confirm_erases_the_file_from_flash(tmp_path):
    # Not just dequeued: gone, or a card fills up with everything ever sent.
    import os
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    ds.peek_file()
    ds.confirm_file()
    assert "a.bin" not in os.listdir(str(tmp_path / "outbox"))


def _drain(ds):
    served = []
    while ds.has_pending():
        served.append(ds.peek_file().get_name())
        ds.confirm_file()
    return served


def test_files_are_served_in_the_order_they_were_queued(tmp_path):
    # Not filename order. These names sort the other way round on purpose.
    ds = _queue(tmp_path)
    for name in ("zulu.bin", "mike.bin", "alpha.bin"):
        ds.enqueue(name, b"x")
    assert _drain(ds) == ["zulu.bin", "mike.bin", "alpha.bin"]


def test_an_mqtt_backlog_drains_in_order(tmp_path):
    # The case that made filename order the wrong rule: the counter in an MQTT name is not
    # zero-padded, so "mq!10!" sorts before "mq!2!" while arriving after it.
    ds = _queue(tmp_path)
    names = ["mq!{}!!sensors%2Ftemp".format(i) for i in range(1, 13)]
    for name in names:
        ds.enqueue(name, b"reading")
    assert _drain(ds) == names


def test_the_order_survives_a_reboot(tmp_path):
    ds = _queue(tmp_path)
    for name in ("zulu.bin", "mike.bin", "alpha.bin"):
        ds.enqueue(name, b"x")
    assert _drain(_queue(tmp_path)) == ["zulu.bin", "mike.bin", "alpha.bin"]


def test_prepare_clears_temps_left_by_an_interrupted_write(tmp_path):
    import os
    ds = _queue(tmp_path)
    with open(str(tmp_path / "outbox" / "half.bin.tmp"), "wb") as f:
        f.write(b"partial")
    assert not ds.has_pending()     # a temp is not a queued file
    ds.prepare()
    assert os.listdir(str(tmp_path / "outbox")) == []


def test_a_repeated_name_is_dropped_rather_than_overwriting(tmp_path):
    # The head may be mid-delivery; replacing its bytes under it would send a file whose
    # metadata described a different one.
    ds = _queue(tmp_path)
    assert ds.enqueue("a.bin", b"first") is True
    assert ds.enqueue("a.bin", b"second") is False
    assert bytes(ds.peek_file().get_content()) == b"first"


def test_a_full_queue_evicts_its_oldest(tmp_path):
    ds = _queue(tmp_path, file_queue_size=2)
    ds.enqueue("001.bin", b"a")
    ds.enqueue("002.bin", b"b")
    ds.enqueue("003.bin", b"c")
    names = []
    while ds.has_pending():
        names.append(ds.peek_file().get_name())
        ds.confirm_file()
    assert names == ["002.bin", "003.bin"]


def test_a_full_queue_never_evicts_the_file_being_delivered(tmp_path):
    # Evicting by age alone would take the file currently on the air, since it is the
    # oldest. The transfer would then complete and confirm against a file that no longer
    # exists, and the next-oldest, which was never sent, would be erased in its place.
    ds = _queue(tmp_path, file_queue_size=2)
    ds.enqueue("001.bin", b"first")
    ds.enqueue("002.bin", b"second")
    assert ds.peek_file().get_name() == "001.bin"       # on the air

    ds.enqueue("003.bin", b"third")                     # full: something has to go
    assert ds.confirm_file().get_name() == "001.bin"    # and it finished cleanly
    assert _drain(ds) == ["003.bin"]                    # 002 was the one dropped


def test_an_index_entry_with_no_file_behind_it_is_forgotten(tmp_path):
    import os
    ds = _queue(tmp_path)
    ds.enqueue("gone.bin", b"x")
    ds.enqueue("kept.bin", b"y")
    os.remove(str(tmp_path / "outbox" / "gone.bin"))     # deleted behind the queue's back
    assert _drain(_queue(tmp_path)) == ["kept.bin"]


def test_a_file_written_but_never_indexed_is_still_sent(tmp_path):
    # The crash window during enqueue: the payload is committed and the index write never
    # happened. The file must be adopted, because losing it is the thing this class exists
    # to prevent. It goes on the end, which is the only place its order can be guessed.
    ds = _queue(tmp_path)
    ds.enqueue("known.bin", b"x")
    with open(str(tmp_path / "outbox" / "orphan.bin"), "wb") as f:
        f.write(b"survived the power cut")
    assert _drain(_queue(tmp_path)) == ["known.bin", "orphan.bin"]


def test_a_file_dropped_in_by_hand_is_picked_up_once_the_queue_drains(tmp_path):
    ds = _queue(tmp_path)
    with open(str(tmp_path / "outbox" / "dropped.bin"), "wb") as f:
        f.write(b"by hand")
    assert not ds.has_pending()      # not noticed yet
    ds.check()                       # the serve loop's pump, on an empty queue
    assert ds.peek_file().get_name() == "dropped.bin"


def test_the_index_is_never_served_as_a_queued_file(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("real.bin", b"x")
    assert _drain(_queue(tmp_path)) == ["real.bin"]


def test_an_empty_payload_is_refused(tmp_path):
    # It has no chunks to request, so it would hold the head of the queue forever.
    ds = _queue(tmp_path)
    assert ds.enqueue("empty.bin", b"") is False
    assert not ds.has_pending()


def test_an_unusable_file_is_dropped_and_the_next_one_served(tmp_path):
    # Something else wrote a zero-length file into the folder. Once adopted it sits at the
    # front of the queue with nothing to send, and it must not wedge everything behind it.
    ds = _queue(tmp_path)
    with open(str(tmp_path / "outbox" / "bad.bin"), "wb") as f:
        f.write(b"")
    ds.check()
    assert ds.has_pending()
    ds.enqueue("good.bin", b"good")
    assert ds.peek_file().get_name() == "good.bin"


def test_add_to_queue_routes_the_base_verb_to_flash(tmp_path):
    ds = _queue(tmp_path)
    ds.add_to_queue(AlLoRa_File(name="v2.bin", content=bytearray(b"legacy"), chunk_size=8))
    assert _queue(tmp_path).has_pending()


def test_legacy_get_next_file_still_pops_destructively(tmp_path):
    # v2 subclasses call this one. It keeps the old at-most-once behavior.
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    assert ds.get_next_file().get_name() == "a.bin"
    assert not ds.has_pending()
    assert ds.get_next_file() is None


def test_the_disk_queue_vouches_that_it_survives_a_restart(tmp_path):
    # What it actually promises is narrower than "durable" suggests: the same name in the
    # queue is the same bytes. enqueue refuses a repeated name and a file leaves only on
    # the peer's confirmation, so a name handed back after a reboot is the file that was
    # being sent. That is what lets an interrupted transfer continue instead of restarting.
    assert _queue(tmp_path).is_durable() is True


# -- the node's serve loop against it -------------------------------------------------------


def test_the_serve_loop_borrows_the_head_without_dequeuing_it(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    edge = _make_edge(tmp_path, datasource=ds)

    edge._pump_datasource()
    assert edge.file.get_name() == "a.bin"
    assert ds.has_pending(), "the file must stay on flash while it is being sent"


def test_a_transfer_that_never_completed_leaves_the_file_queued(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    edge = _make_edge(tmp_path, datasource=ds)
    edge._pump_datasource()

    edge._retire_file(False)        # timed out, link died, node rebooted
    assert edge.file is None
    assert ds.has_pending()

    edge._pump_datasource()         # and it is served again
    assert edge.file.get_name() == "a.bin"


def test_only_the_peers_confirmation_drops_the_file(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("a.bin", b"aaa")
    edge = _make_edge(tmp_path, datasource=ds)
    edge._pump_datasource()

    edge._retire_file(True)
    assert edge.file is None
    assert not ds.has_pending()


def test_a_file_installed_by_hand_is_not_the_queues_business(tmp_path):
    # set_file is the caller's own file. Finishing with it must not credit the queue,
    # or an example that mixes the two would silently eat a queued reading per send.
    ds = _queue(tmp_path)
    ds.enqueue("queued.bin", b"aaa")
    edge = _make_edge(tmp_path, datasource=ds)

    edge.set_file(AlLoRa_File(name="own.bin", content=bytearray(b"mine"), chunk_size=8))
    edge._retire_file(True)
    assert ds.has_pending()
    assert ds.peek_file().get_name() == "queued.bin"


def test_the_queue_is_drained_in_order_across_rounds(tmp_path):
    ds = _queue(tmp_path)
    ds.enqueue("001.bin", b"a")
    ds.enqueue("002.bin", b"b")
    edge = _make_edge(tmp_path, datasource=ds)

    edge._pump_datasource()
    assert edge.file.get_name() == "001.bin"
    edge._retire_file(True)
    edge._pump_datasource()
    assert edge.file.get_name() == "002.bin"
    edge._retire_file(True)
    edge._pump_datasource()
    assert edge.file is None

"""DataSource base — the input boundary, now dual-runtime and cooperative.

v2 shipped DataSource as a CPython-unimportable module (module-level utime + a _thread
polling loop), so host tooling and CI could never touch the ingest half. The base now
lives in the DataSources package (the input-boundary mirror of DataSinks), imports on
both runtimes, and keeps the legacy surface intact: the top-level import path, the
thread API, and get_next_file's destructive pop all behave as v2 student subclasses
expect.

One thing it does not keep is v2's `file_chunk_size`: how a file is cut belongs to the node,
which clamps the number against its framing, and a copy held here drifted from it. See
test_chunk_size_one_store.py.
"""


def test_base_imports_and_constructs_on_cpython():
    from AlLoRa.DataSources.DataSource import DataSource
    ds = DataSource()
    assert ds.has_pending() is False


def _file(name, payload=b"x"):
    from AlLoRa.File import AlLoRa_File
    return AlLoRa_File(name=name, content=bytearray(payload), chunk_size=8)


def _base(**kwargs):
    from AlLoRa.DataSources.DataSource import DataSource
    return DataSource(**kwargs)


def test_peek_retains_the_head_until_confirm():
    # The downlink contract: a served file leaves the queue only when its delivery is
    # confirmed, so a failed delegation retries the same file (at-least-once).
    ds = _base()
    ds.add_to_queue(_file("a"))
    assert ds.has_pending()
    assert ds.peek_file().get_name() == "a"
    assert ds.peek_file().get_name() == "a"
    assert ds.confirm_file().get_name() == "a"
    assert not ds.has_pending()
    assert ds.peek_file() is None
    assert ds.confirm_file() is None


def test_a_queue_that_fills_up_mid_delivery_does_not_confirm_the_wrong_file():
    # peek hands out the head, then the queue overflows and evicts that same head. The
    # confirm that follows must drop the file that was served, not the one that replaced
    # it at position zero, which has not been sent.
    ds = _base(file_queue_size=2)
    ds.add_to_queue(_file("a"))
    ds.add_to_queue(_file("b"))
    assert ds.peek_file().get_name() == "a"
    ds.add_to_queue(_file("c"))     # evicts "a" while it is in flight
    ds.confirm_file()
    assert [f.get_name() for f in ds.file_queue] == ["b", "c"]


def test_the_base_queue_does_not_vouch_for_surviving_a_restart():
    # Durability is declared, not detected, because only the boundary knows its own
    # storage. The base queue lives in RAM and genuinely loses its files on a reboot, so
    # it says nothing and the node restarts an interrupted transfer rather than resuming
    # one. The default has to be the safe answer: a source that never thought about this
    # question must not be taken to have answered it.
    assert _base().is_durable() is False


def test_legacy_get_next_file_still_pops_destructively():
    ds = _base()
    ds.add_to_queue(_file("a"))
    assert ds.get_next_file().get_name() == "a"
    assert ds.get_next_file() is None


def test_queue_dedupes_by_name_and_drops_oldest_on_overflow():
    # Pin of the v2 semantics: a repeated name is dropped, a full queue evicts its head.
    ds = _base(file_queue_size=2)
    ds.add_to_queue(_file("a"))
    ds.add_to_queue(_file("a"))
    assert [f.get_name() for f in ds.file_queue] == ["a"]
    ds.add_to_queue(_file("b"))
    ds.add_to_queue(_file("c"))
    assert [f.get_name() for f in ds.file_queue] == ["b", "c"]


def test_cooperative_lifecycle_verbs_are_noops_on_the_base():
    # check() is the non-blocking pump a node loop calls each round; close() releases
    # whatever prepare() acquired. The base needs neither, so both must be safe no-ops.
    ds = _base()
    ds.check()
    ds.close()
    assert not ds.has_pending()

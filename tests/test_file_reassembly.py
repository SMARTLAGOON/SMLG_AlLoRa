"""Reassembly integrity — the silent-corruption case.

v2's `add_chunk(order, chunk)` ignored `order` and appended in arrival order, so a
file only reassembled correctly if every chunk arrived exactly once and in sequence.
Any reorder or duplicate silently corrupted the result. v3 makes the write
positioned (`seek(order * chunk_size)`) and idempotent, so the signature tells the
truth: `order` actually places the chunk.
"""
from math import ceil

from AlLoRa.File import AlLoRa_File


def _split(payload, chunk_size):
    return {i: payload[i * chunk_size:(i + 1) * chunk_size]
            for i in range(ceil(len(payload) / chunk_size))}


def test_add_chunk_reassembles_out_of_order_and_idempotent(tmp_path):
    chunk_size = 8
    # 43 bytes -> 6 chunks: five full (8 B) + a short tail (3 B).
    payload = bytes(i % 256 for i in range(43))
    chunks = _split(payload, chunk_size)
    n_chunks = len(chunks)

    f = AlLoRa_File(name="ooo.bin", length=n_chunks,
                    chunk_size=chunk_size, path=str(tmp_path))

    # Scrambled order, with chunk 2 delivered twice (a retransmission/duplicate)
    # and the short tail (5) arriving before middle chunks.
    arrival = [3, 0, 5, 1, 2, 4, 2]
    for i in arrival:
        f.add_chunk(i, chunks[i])

    # Idempotent: the duplicate must not leave a chunk "missing" or double-count.
    assert f.get_missing_chunks() == []
    # Positioned: bytes land at order * chunk_size regardless of arrival order.
    assert f.get_content() == payload


def test_add_chunk_refuses_a_chunk_that_contradicts_the_advertised_total(tmp_path):
    """A peer that changes file mid-transfer must not be able to splice.

    Completeness used to be counted, not measured: every chunk index present meant
    the file was done, so a full-size chunk standing in for the real short tail was
    invisible. METADATA advertises the exact byte count, which fixes every chunk's
    expected length in advance, so a chunk of the wrong length is refused on arrival.
    """
    chunk_size = 200
    payload = bytes(i % 256 for i in range(420))    # 3 chunks: 200 / 200 / 20
    chunks = _split(payload, chunk_size)

    f = AlLoRa_File(name="foxtrot.bin", length=len(chunks), chunk_size=chunk_size,
                    total_len=len(payload), path=str(tmp_path))
    assert f.add_chunk(0, chunks[0])
    assert f.add_chunk(1, chunks[1])

    # The serving node reset here and came back holding a different file, so index 2
    # is answered with a full 200-byte chunk where 20 bytes were advertised.
    assert f.add_chunk(2, bytes(200)) is False
    assert f.get_missing_chunks() == [2], "a refused chunk must stay missing"


def test_add_chunk_accepts_the_genuine_short_tail(tmp_path):
    chunk_size = 200
    payload = bytes(i % 256 for i in range(420))
    chunks = _split(payload, chunk_size)

    f = AlLoRa_File(name="tail.bin", length=len(chunks), chunk_size=chunk_size,
                    total_len=len(payload), path=str(tmp_path))
    for i in range(len(chunks)):
        assert f.add_chunk(i, chunks[i])

    assert f.get_missing_chunks() == []
    assert f.get_content() == payload


def test_add_chunk_checks_by_index_not_by_arrival_order(tmp_path):
    """The check must survive a burst. Windowed selective-repeat delivers chunks out
    of order, so the expected length is keyed on the index, exactly like the
    positioned write, and never on how many chunks have landed so far."""
    chunk_size = 200
    payload = bytes(i % 256 for i in range(420))
    chunks = _split(payload, chunk_size)

    f = AlLoRa_File(name="burst.bin", length=len(chunks), chunk_size=chunk_size,
                    total_len=len(payload), path=str(tmp_path))

    # The short tail arrives first and is correct; a full-size chunk 0 follows.
    assert f.add_chunk(2, chunks[2])
    assert f.add_chunk(0, chunks[0])
    # A short chunk in a non-final position is wrong however early it arrives.
    assert f.add_chunk(1, chunks[1][:20]) is False
    assert f.get_missing_chunks() == [1]


def test_add_chunk_without_an_advertised_total_refuses_nothing(tmp_path):
    """v2 METADATA carries only a chunk count, so there is no byte total to check
    against. The guard degrades to a no-op rather than failing the transfer."""
    chunk_size = 200
    f = AlLoRa_File(name="v2.bin", length=3, chunk_size=chunk_size,
                    path=str(tmp_path))

    assert f.add_chunk(0, bytes(200))
    assert f.add_chunk(1, bytes(200))
    assert f.add_chunk(2, bytes(200))
    assert f.get_missing_chunks() == []


def test_add_chunk_in_order_still_correct(tmp_path):
    chunk_size = 16
    payload = bytes((i * 7) % 256 for i in range(100))  # 7 chunks (6 full + 4 B tail)
    chunks = _split(payload, chunk_size)

    f = AlLoRa_File(name="seq.bin", length=len(chunks),
                    chunk_size=chunk_size, path=str(tmp_path))
    for i in range(len(chunks)):
        f.add_chunk(i, chunks[i])

    assert f.get_missing_chunks() == []
    assert f.get_content() == payload

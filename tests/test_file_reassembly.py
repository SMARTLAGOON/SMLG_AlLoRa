"""Reassembly integrity — the corruption case (ADR 0001 §6).

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

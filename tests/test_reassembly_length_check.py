"""The endpoint refuses a chunk that disagrees with the advertised file length.

Found on hardware on 2026-08-09: a Hub was mid-transfer of a 420-byte file when the
serving board reset. It saved 600 bytes under that name and reported success, with
contents spliced from two different files.

Nothing caught it because a chunk request carries an index and nothing else, so there
is no binding between the answer and the file being assembled, and the collector
declared the file received once no index was missing. Completeness was counted, never
measured. v3 METADATA already carries the exact byte total for precisely this purpose;
these tests pin the endpoint to actually using it.
"""
from math import ceil

from AlLoRa.Digital_Endpoint import Digital_Endpoint


CHUNK_SIZE = 200
PAYLOAD = bytes(i % 256 for i in range(420))    # 3 chunks: 200 / 200 / 20


def _split(payload, chunk_size):
    return [payload[i * chunk_size:(i + 1) * chunk_size]
            for i in range(ceil(len(payload) / chunk_size))]


def _endpoint_receiving(tmp_path, total_len=len(PAYLOAD)):
    endpoint = Digital_Endpoint(name="edge", mac_address="9eeff0dc",
                                active=True, session_id=7)
    endpoint.set_metadata((3, "foxtrot.bin"), None, False, path=str(tmp_path),
                          chunk_size=CHUNK_SIZE, total_len=total_len)
    return endpoint


def test_a_chunk_contradicting_the_advertised_length_is_refused(tmp_path):
    chunks = _split(PAYLOAD, CHUNK_SIZE)
    endpoint = _endpoint_receiving(tmp_path)

    for i in (0, 1):
        assert endpoint.get_next_chunk() == i
        assert endpoint.set_data(chunks[i], None, False) is None

    # The serving node reset and came back holding a different file, so the request for
    # index 2 is answered with a full 200-byte chunk where 20 bytes were advertised.
    assert endpoint.get_next_chunk() == 2
    assert endpoint.set_data(bytes(CHUNK_SIZE), None, False) is Digital_Endpoint.CHUNK_REFUSED


def test_a_refused_chunk_never_completes_the_file(tmp_path):
    chunks = _split(PAYLOAD, CHUNK_SIZE)
    endpoint = _endpoint_receiving(tmp_path)

    endpoint.get_next_chunk(); endpoint.set_data(chunks[0], None, False)
    endpoint.get_next_chunk(); endpoint.set_data(chunks[1], None, False)
    endpoint.get_next_chunk(); endpoint.set_data(bytes(CHUNK_SIZE), None, False)

    # The index the bad answer came for is still outstanding, so nothing can be saved:
    # a spliced file must never reach a DataSink, and must never be reported delivered.
    assert endpoint.get_next_chunk() == 2
    assert endpoint.state == Digital_Endpoint.PROCESS_CHUNK_STATE


def test_the_genuine_short_tail_completes_the_file(tmp_path):
    chunks = _split(PAYLOAD, CHUNK_SIZE)
    endpoint = _endpoint_receiving(tmp_path)

    for i in (0, 1):
        endpoint.get_next_chunk()
        assert endpoint.set_data(chunks[i], None, False) is None

    endpoint.get_next_chunk()
    received = endpoint.set_data(chunks[2], None, False)

    assert received is not None and received is not Digital_Endpoint.CHUNK_REFUSED
    assert received.get_content() == PAYLOAD


def test_replacing_a_reassembly_releases_the_one_it_abandons(tmp_path):
    """An abandoned reassembly must not leave its writer and temp file behind.

    Re-opening a transfer part-way through used to happen only when a session had already
    been given up for dead, and the one call site that did it cleaned up by hand. It is
    ordinary now: a chunk refused for its length, or a source that answers with METADATA
    because it does not remember announcing the file. On a board the descriptor table is
    tiny and the flash is smaller, so the release belongs where the replacement happens.
    """
    endpoint = _endpoint_receiving(tmp_path)
    endpoint.get_next_chunk()
    endpoint.set_data(_split(PAYLOAD, CHUNK_SIZE)[0], None, False)
    abandoned = tmp_path / "Temp" / "foxtrot.bin.tmp"
    assert abandoned.exists(), "test setup: no reassembly to abandon"

    endpoint.set_metadata((3, "hotel.bin"), None, False, path=str(tmp_path),
                          chunk_size=CHUNK_SIZE, total_len=len(PAYLOAD))

    assert not abandoned.exists(), "the replaced reassembly leaked its temp file"
    assert endpoint.get_current_file().get_name() == "hotel.bin"


def test_re_pulling_the_same_file_keeps_the_reassembly_it_just_opened(tmp_path):
    """Releasing the old reassembly must happen before the new one is opened.

    The same file gets re-pulled whenever the collector rewinds: a sink that failed, a
    chunk refused, a source that re-announced. Both reassemblies then use the same temp
    path, so releasing the old one *after* opening the new one deletes the buffer that was
    just created. Nothing fails at the time, because an open handle survives the unlink and
    the writes keep going, and the transfer only comes apart at the end: every chunk
    received, and no file to rename into place.
    """
    chunks = _split(PAYLOAD, CHUNK_SIZE)
    endpoint = _endpoint_receiving(tmp_path)
    endpoint.get_next_chunk()
    endpoint.set_data(chunks[0], None, False)
    endpoint.get_current_file().discard()      # the sink raised; drop it and re-pull

    endpoint.set_metadata((3, "foxtrot.bin"), None, False, path=str(tmp_path),
                          chunk_size=CHUNK_SIZE, total_len=len(PAYLOAD))
    for i in (0, 1, 2):
        endpoint.get_next_chunk()
        received = endpoint.set_data(chunks[i], None, False)

    assert received is not None and received is not Digital_Endpoint.CHUNK_REFUSED
    received.save(str(tmp_path / "Results"))
    assert (tmp_path / "Results" / "foxtrot.bin").read_bytes() == PAYLOAD


def test_dropping_a_reassembly_releases_it_too(tmp_path):
    # The other half: the collector re-opening the transfer clears the endpoint rather
    # than replacing it, and that path has to release the buffer just the same.
    endpoint = _endpoint_receiving(tmp_path)
    abandoned = tmp_path / "Temp" / "foxtrot.bin.tmp"
    assert abandoned.exists()

    endpoint.set_current_file(None)

    assert not abandoned.exists(), "the dropped reassembly leaked its temp file"


def test_a_v2_peer_advertises_no_total_so_nothing_is_refused(tmp_path):
    """v2 METADATA carries a chunk count and no byte total, so there is nothing to
    check against. The guard has to stay out of the way rather than fail the link."""
    endpoint = _endpoint_receiving(tmp_path, total_len=None)

    for _ in range(2):
        endpoint.get_next_chunk()
        assert endpoint.set_data(bytes(CHUNK_SIZE), None, False) is None

    endpoint.get_next_chunk()
    received = endpoint.set_data(bytes(CHUNK_SIZE), None, False)
    assert received is not None and received is not Digital_Endpoint.CHUNK_REFUSED

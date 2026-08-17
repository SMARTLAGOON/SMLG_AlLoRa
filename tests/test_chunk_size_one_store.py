"""Chunk size has one store, and the node owns it.

Two things used to decide how a file was cut into chunks, and they disagreed. The node reads
`chunk_size` from config and clamps it with `calculate_max_chunk_size()`, which subtracts what
the codec spends on framing, so the node's number is the only one that knows the posture it is
speaking. The DataSource took `file_chunk_size` in its constructor, set by whoever built it
before the node existed, never clamped, and stamped it into every file it handed over. In the
open posture the two landed on the same value by luck. In the sealed posture the codec spends
8 bytes on a sealed header and a tag, the node clamps lower, the constant did not, and the file
went out cut into chunks too big for the frame carrying them. One 32 KiB secure arm on
2026-08-11 paid for that.

The store is singular now. A queue holds bytes and a name, not a chunk size; a file may be
built before anything knows what will carry it; and the node stamps its own clamped value at
the moment it installs a file, which is every pump. So a retune reaches the next file off the
queue rather than the next reboot: a signed RF_CONFIG can carry a new `cks` and the node
re-clamps, and any copy kept elsewhere would drift again from the first one.

Stating a size is still allowed, by building the file and handing it to `set_file`. An
oversized one is refused, loudly, and never quietly re-cut: a caller who asked for 255 and
silently got 247 has been handed a number it can neither see nor trust.
"""
import json
from math import ceil

import pytest

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.DataSources.MQTT_DataSource import MQTT_DataSource
from AlLoRa.File import AlLoRa_File
from AlLoRa.Nodes.Edge import Edge
from test_hub_endpoints import _make_hub, _node
from test_mqtt_datasource import Fake_client

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"

# Configured above every posture's ceiling, so the clamp always has work to do and the number
# the node lands on is never the number a caller would have guessed from the config file.
CONFIGURED = 255
OPEN_CEILING = 249          # 255 - the v3 open header
SEALED_CEILING = 247        # 255 - (the sealed header + the tag)

PAYLOAD = bytes(i % 256 for i in range(1000))


def _write_config(path, security_mode="open", chunk_size=CONFIGURED):
    config = {
        "name": "one-store", "chunk_size": chunk_size, "mesh_mode": False,
        "protocol_version": 3, "security_mode": security_mode,
        "session_id": SESSION_ID, "debug": False,
        "connector": {"sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
                      "tx_power": 14, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def _edge(tmp_path, name="edge.json", datasource=None, **config):
    path = _write_config(str(tmp_path / name), **config)
    conn, _ = Loopback_connector.create_pair(EDGE_MAC, HUB_MAC)
    return Edge(conn, config_file=path, datasource=datasource)


def _disk_queue(tmp_path, name="Outbox"):
    queue = Disk_DataSource(queue_path=str(tmp_path / name))
    queue.prepare()
    return queue


# -- the queue holds no number ------------------------------------------------------------


def test_a_queue_states_no_chunk_size_of_its_own(tmp_path):
    # It hands back bytes under a name and says nothing about how they should be cut. The
    # posture the frame will travel in is not something a folder can know.
    queue = _disk_queue(tmp_path)
    queue.enqueue("reading.bin", PAYLOAD)

    assert queue.peek_file().chunk_size is None


def test_a_broker_message_states_no_chunk_size_either(tmp_path):
    # The MQTT source builds its file the moment the message lands, which on paho is a
    # network thread that can run before the node has pumped anything. There is no value it
    # could stamp then that would still be right at delivery, so it stamps none.
    source = MQTT_DataSource(topics=("sensors/#",), client=Fake_client())
    source.prepare()
    source._client.pending.append((b"sensors/t", b"21.5"))
    source.check()

    assert source.peek_file().chunk_size is None


def test_the_v2_constructor_argument_is_refused_rather_than_ignored(tmp_path):
    # Gone, not deprecated. Accepting the number and then overriding it would read to the
    # caller as having been honoured, which is the drift this change removes.
    with pytest.raises(TypeError):
        DataSource(file_chunk_size=200)
    with pytest.raises(TypeError):
        Disk_DataSource(file_chunk_size=200, queue_path=str(tmp_path / "Outbox"))
    with pytest.raises(TypeError):
        MQTT_DataSource(file_chunk_size=200, client=Fake_client())


# -- the node stamps, on every pump -------------------------------------------------------


def test_a_file_off_the_queue_is_cut_at_the_nodes_clamped_size(tmp_path):
    queue = _disk_queue(tmp_path)
    queue.enqueue("reading.bin", PAYLOAD)
    edge = _edge(tmp_path, datasource=queue)

    edge._pump_datasource()

    assert edge.get_chunk_size() == OPEN_CEILING
    assert edge.file.chunk_size == OPEN_CEILING
    assert edge.file.get_length() == ceil(len(PAYLOAD) / OPEN_CEILING)


def test_the_sealed_posture_no_longer_builds_chunks_too_big_for_its_frame(tmp_path):
    """The 2026-08-11 regression, at the boundary where it was paid for.

    The sealed header and the tag cost 8 bytes the open posture does not spend, so a queue
    holding the open number cut every file 2 bytes over what the sealed frame could carry.
    """
    queue = _disk_queue(tmp_path)
    queue.enqueue("reading.bin", PAYLOAD)
    edge = _edge(tmp_path, "sealed.json", datasource=queue, security_mode="secure")

    edge._pump_datasource()

    assert edge.file.chunk_size == SEALED_CEILING
    assert (edge.file.chunk_size + edge.connector.codec.payload_overhead()
            <= edge.connector.get_max_payload_size())


def test_a_retune_reaches_the_next_file_off_the_queue(tmp_path):
    """Why the size is supplied on every pump and not once at attach.

    A signed RF_CONFIG can carry a new `cks`, and the node re-clamps when it applies one. A
    copy stored on the queue at attach would be right exactly until the first retune, and
    would then be wrong for every file after it with nothing saying so.
    """
    queue = _disk_queue(tmp_path)
    queue.enqueue("first.bin", PAYLOAD)
    queue.enqueue("second.bin", PAYLOAD)
    edge = _edge(tmp_path, datasource=queue)

    edge._pump_datasource()
    assert edge.file.chunk_size == OPEN_CEILING
    edge._retire_file(True)

    assert edge.change_rf_config({"cks": 100}) is True
    edge._pump_datasource()

    assert edge.file.get_name() == "second.bin"
    assert edge.file.chunk_size == 100


def test_the_head_is_re_cut_when_a_retune_lands_between_two_attempts(tmp_path):
    # An undelivered file stays queued and is served again. The queue caches the head it
    # built, so the second attempt would otherwise go out cut at the size the first one used.
    queue = _disk_queue(tmp_path)
    queue.enqueue("reading.bin", PAYLOAD)
    edge = _edge(tmp_path, datasource=queue)

    edge._pump_datasource()
    edge._retire_file(False)            # the link died; nothing was confirmed
    edge.change_rf_config({"cks": 100})
    edge._pump_datasource()

    assert edge.file.get_name() == "reading.bin"
    assert edge.file.chunk_size == 100


# -- what a caller may still state --------------------------------------------------------


def test_a_file_that_states_no_size_is_stamped_by_the_node(tmp_path):
    # The shape a deployment should write: hand over the bytes, let the node say how they
    # travel. There is then no second number to keep in step with the radio.
    edge = _edge(tmp_path)

    edge.set_file(AlLoRa_File(name="hello.bin", content=bytearray(PAYLOAD)))

    assert edge.file.chunk_size == OPEN_CEILING
    assert edge.file.get_length() == ceil(len(PAYLOAD) / OPEN_CEILING)


def test_a_size_the_radio_can_carry_is_left_exactly_as_asked(tmp_path):
    # Smaller than the ceiling is a legitimate choice (a caller matching a record length,
    # a bench sweep), so it is neither raised to the ceiling nor complained about.
    edge = _edge(tmp_path)

    edge.set_file(AlLoRa_File(name="hello.bin", content=bytearray(PAYLOAD), chunk_size=100))

    assert edge.file.chunk_size == 100


def test_an_oversized_size_is_refused_loudly_and_never_quietly_re_cut(tmp_path):
    edge = _edge(tmp_path)
    file = AlLoRa_File(name="hello.bin", content=bytearray(PAYLOAD), chunk_size=CONFIGURED)

    with pytest.raises(ValueError) as refusal:
        edge.set_file(file)

    message = str(refusal.value)
    assert str(CONFIGURED) in message and str(OPEN_CEILING) in message
    assert edge.file is None             # and nothing was installed on the way out


def test_a_refused_file_is_not_touched_on_its_way_out(tmp_path):
    # The refusal comes before anything is written to the caller's file. Installing resets
    # the delivery state, and doing that to a file that was then refused would leave the
    # caller holding an object this node had quietly altered.
    edge = _edge(tmp_path)
    file = AlLoRa_File(name="hello.bin", content=bytearray(PAYLOAD), chunk_size=CONFIGURED)
    file.metadata_sent = True

    with pytest.raises(ValueError):
        edge.set_file(file)

    assert file.chunk_size == CONFIGURED
    assert file.metadata_sent is True


def test_a_file_being_reassembled_refuses_to_be_re_cut(tmp_path):
    # The stamp belongs to the sending side alone. On a reassembly file the chunk size is the
    # *sender's* announced one, and every chunk already on the card sits at an offset derived
    # from it, so re-cutting would renumber bytes that are already written.
    receiving = AlLoRa_File(name="incoming.bin", length=4, chunk_size=249,
                            total_len=1000, path=str(tmp_path))

    with pytest.raises(ValueError):
        receiving.change_chunk_size(100)

    assert receiving.chunk_size == 249


# -- the downlink, which is the same rule on the other direction ---------------------------


def test_the_hub_cuts_its_downlink_at_its_own_size(tmp_path):
    hub = _make_hub(tmp_path, nodes=[_node("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]

    hub.queue_downlink(endpoint, AlLoRa_File(name="ctrl.bin", content=bytearray(b"x" * 500)))
    queued = hub._downlink[endpoint.session_id].peek_file()
    assert queued.chunk_size is None            # still undecided while it waits

    hub._serve_from_boundary(queued)            # what delegation does at the boundary
    assert hub.file.chunk_size == hub.get_chunk_size()


def test_an_oversized_downlink_is_refused_at_the_queue_call(tmp_path):
    # Refused where the mistake is. Left to the delegation boundary it would surface visits
    # later, inside the drive loop, as a failure of the endpoint rather than of the caller.
    hub = _make_hub(tmp_path, nodes=[_node("edge", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]
    oversized = AlLoRa_File(name="ctrl.bin", content=bytearray(b"x" * 500), chunk_size=255)

    with pytest.raises(ValueError):
        hub.queue_downlink(endpoint, oversized)

    assert not hub.downlink_pending(endpoint)

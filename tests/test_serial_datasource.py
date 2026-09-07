"""Serial_DataSource: a Raspberry Pi pushing whole files down a cable to the board.

The deployment these pin is the GNSS rig: a Pi captures NMEA, compresses it, and sends the
archive over UART to a T3S3 that holds the AlLoRa protocol and the outbox. The board is the
node; the Pi is a producer that only knows how to hand over a file.

What makes this different from every other DataSource is that the receive is long. A file
takes minutes at 9600 baud, and `check()` shares the radio loop, so the transfer has to
advance a little on each call and return. The failure that costs data is a half-arrived file
being adopted as a whole one, so most of what is below is about the moments a transfer can be
cut off: mid-chunk, at the end, by a Pi that stops talking, by a reboot.
"""
import binascii
import json
import os

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.DataSources.Serial_DataSource import Serial_DataSource
from AlLoRa.Nodes.Edge import Edge

SESSION_ID = 42
EDGE_MAC = "a1a1a1a1"
HUB_MAC = "b2b2b2b2"

# What the Pi actually names its captures, and what the old board wrote to the card for it.
PI_NAME = "2026-03-09_13-25-00.tar.xz"
SHORT_NAME = "260309-132500.xz"


# -- the cable ------------------------------------------------------------------------------


class _Cable:
    """A UART with a Pi on the far end: the board's three verbs, plus the Pi's two.

    `any`/`read`/`write` are the whole port surface Serial_DataSource is written against, and
    read never waits for bytes that have not arrived, which is what the real port is
    configured to do with `timeout=0`.
    """

    def __init__(self):
        self.to_board = bytearray()
        self.from_board = bytearray()

    # the board's end
    def any(self):
        return len(self.to_board)

    def read(self, n=None):
        if n is None:
            n = len(self.to_board)
        if not self.to_board:
            return None
        data = bytes(self.to_board[:n])
        del self.to_board[:n]
        return data

    def write(self, data):
        self.from_board += data

    # the Pi's end
    def send(self, data):
        self.to_board += data

    def take_line(self):
        i = self.from_board.find(b"\n")
        if i < 0:
            return None
        line = bytes(self.from_board[:i])
        del self.from_board[:i + 1]
        return line.strip()


class _Pi:
    """The producer, driven one move at a time and never talking out of turn.

    It sends only what the board has answered for, so these tests exercise the handshake
    rather than a recording of it: START waits for LISTEN, every chunk waits for its ACK, and
    a NACK resends the same chunk instead of moving on.
    """

    def __init__(self, cable, name=PI_NAME, payload=b"", chunk=512, name_length=26,
                 corrupt=()):
        self.cable = cable
        self.name = name
        self.payload = payload
        self.chunk = chunk
        self.name_length = name_length
        self.corrupt = set(corrupt)
        self.attempted = set()
        self.sent = 0
        self.state = "start"
        self.acks = 0
        self.nacks = 0
        self.confirmed = False

    def header(self):
        crc = binascii.crc32(self.payload) & 0xFFFFFFFF
        return (b"%8d\n" % len(self.payload)
                + self.name.encode().ljust(self.name_length)
                + b"\n"
                + b"%010d\n" % crc)

    def step(self):
        """One move, if the board has left one open. True if anything was sent."""
        return getattr(self, "_" + self.state)()

    def _start(self):
        self.cable.send(b"START\n")
        self.state = "listen"
        return True

    def _listen(self):
        if self.cable.take_line() != b"LISTEN":
            return False
        self.cable.send(self.header())
        self.state = "chunk"
        return True

    def _chunk(self):
        if self.sent >= len(self.payload):
            self.cable.send(b"END\n")
            self.state = "ok"
            return True
        body = self.payload[self.sent:self.sent + self.chunk]
        index = self.sent // self.chunk
        crc = binascii.crc32(body) & 0xFFFFFFFF
        if index in self.corrupt and index not in self.attempted:
            crc = (crc + 1) & 0xFFFFFFFF          # a bit flipped on the wire
        self.attempted.add(index)
        self.cable.send(b"%d\n" % crc + body)
        self.state = "ack"
        return True

    def _ack(self):
        line = self.cable.take_line()
        if line is None:
            return False
        if line == b"ACK":
            self.acks += 1
            self.sent += min(self.chunk, len(self.payload) - self.sent)
        else:
            self.nacks += 1
        self.state = "chunk"
        return True

    def _ok(self):
        if self.cable.take_line() != b"OK":
            return False
        self.confirmed = True
        self.state = "done"
        return True

    def _done(self):
        return False


class _Clock:
    """A hand-wound millisecond clock, so a stalled link can be waited out in a test."""

    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms


def _source(tmp_path, cable, clock=None, name="outbox", **kwargs):
    ds = Serial_DataSource(queue_path=str(tmp_path / name), uart=cable,
                           clock=clock, **kwargs)
    ds.prepare()
    return ds


def _run(ds, pi, rounds=400):
    """Turn the crank: the Pi moves, the board pumps, until the transfer settles."""
    for _ in range(rounds):
        moved = pi.step()
        ds.check()
        if pi.state == "done":
            ds.check()
            return True
        if not moved and pi.state in ("listen", "ack", "ok"):
            # The Pi is waiting on the board and the board sent nothing: give it another
            # round to make progress, and stop if it never does.
            ds.check()
    return pi.state == "done"


def _payload(n):
    return bytes((i * 7 + 11) & 0xFF for i in range(n))


def _queued(ds):
    """What the queue itself counts as a file on the card, which is what `_reconcile` adopts."""
    return sorted(ds._names_on_disk())


# -- a file crossing the cable --------------------------------------------------------------


def test_a_whole_file_arrives_and_is_queued(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    pi = _Pi(cable, payload=_payload(1500))

    assert _run(ds, pi)
    assert pi.confirmed                      # the Pi was told it may let go of its copy
    assert ds.has_pending()
    file = ds.peek_file()
    assert file.get_name() == SHORT_NAME
    assert bytes(file.get_content()) == _payload(1500)


def test_a_file_shorter_than_one_chunk_still_arrives(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    pi = _Pi(cable, payload=b"one chunk and not a full one")

    assert _run(ds, pi)
    assert bytes(ds.peek_file().get_content()) == b"one chunk and not a full one"


def test_the_transfer_survives_arriving_one_byte_at_a_time(tmp_path):
    # The point of the whole rewrite. The v2 receive was one blocking call with 2000 ms UART
    # timeouts inside it; this one has to be able to stop anywhere and pick up where it left
    # off, because the radio loop is calling it between chunks.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    payload = _payload(1200)
    pi = _Pi(cable, payload=payload)

    # Hold the cable to a single byte per round, and pump the board far more often than the
    # Pi speaks, which is exactly the shape of a slow link under a busy radio.
    original_read = cable.read
    cable.read = lambda n=None: original_read(1)

    assert _run(ds, pi, rounds=20000)
    assert bytes(ds.peek_file().get_content()) == payload


def test_one_check_does_not_swallow_a_whole_file(tmp_path):
    # `check()` shares the radio loop, so the receive is bounded per call whatever is waiting
    # in the port. A board that finished a 20 KB file inside one call went deaf while it did,
    # which is precisely the failure the v2 blocking receive had and the reason it needed a
    # thread of its own.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    payload = _payload(20000)
    cable.send(b"START\n")
    ds.check()
    assert cable.take_line() == b"LISTEN"

    # The whole file in the port at once, which is how a driver buffer looks to a node that has
    # been busy on the radio. Nothing here waits for an ACK.
    blob = bytearray(_Pi(cable, payload=payload).header())
    at = 0
    while at < len(payload):
        body = payload[at:at + 512]
        blob += b"%d\n" % (binascii.crc32(body) & 0xFFFFFFFF) + body
        at += 512
    blob += b"END\n"
    cable.send(bytes(blob))

    ds.check()
    assert not ds.has_pending()               # one call is nowhere near enough
    rounds = 1
    while not ds.has_pending() and rounds < 500:
        ds.check()
        rounds += 1
    assert ds.has_pending()
    assert rounds > 10                        # it really was spread across the radio's rounds
    assert bytes(ds.peek_file().get_content()) == payload


# -- the ways a transfer ends badly ---------------------------------------------------------


def test_a_half_arrived_file_is_never_queued(tmp_path):
    # The failure that costs data: a truncated capture believed to be a short one. Nothing
    # downstream can tell those apart, so the queue must never hold one.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    pi = _Pi(cable, payload=_payload(3000))
    for _ in range(6):                        # the Pi is unplugged part way through
        pi.step()
        ds.check()

    assert pi.state != "done"
    assert not ds.has_pending()
    # The bytes so far are on the card, and that is fine: they are under a .tmp, which the
    # queue does not see and cannot adopt. The invariant is that nothing is queued.
    assert os.listdir(ds.queue_path) == [SHORT_NAME + ".tmp"]
    assert _queued(ds) == []


def test_a_reboot_mid_transfer_leaves_nothing_to_adopt(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    pi = _Pi(cable, payload=_payload(3000))
    for _ in range(6):
        pi.step()
        ds.check()

    rebooted = _source(tmp_path, _Cable())
    assert not rebooted.has_pending()
    assert _queued(rebooted) == []            # prepare() swept the partial away


def test_a_size_mismatch_is_refused_and_the_pi_keeps_its_copy(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    payload = _payload(900)
    pi = _Pi(cable, payload=payload)
    pi.header = lambda: (b"%8d\n" % (len(payload) + 64)      # claims more than it sends
                         + PI_NAME.encode().ljust(26) + b"\n"
                         + b"%010d\n" % (binascii.crc32(payload) & 0xFFFFFFFF))

    _run(ds, pi, rounds=60)
    assert not ds.has_pending()
    assert not pi.confirmed                   # no OK, so the Pi still has the file to resend


def test_a_whole_file_checksum_mismatch_is_refused(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    payload = _payload(900)
    pi = _Pi(cable, payload=payload)
    pi.header = lambda: (b"%8d\n" % len(payload)
                         + PI_NAME.encode().ljust(26) + b"\n"
                         + b"%010d\n" % ((binascii.crc32(payload) + 1) & 0xFFFFFFFF))

    _run(ds, pi, rounds=60)
    assert not ds.has_pending()
    assert not pi.confirmed


def test_a_corrupt_chunk_is_nacked_and_the_resend_is_taken(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    payload = _payload(1500)
    pi = _Pi(cable, payload=payload, corrupt=(1,))

    assert _run(ds, pi)
    assert pi.nacks == 1
    assert bytes(ds.peek_file().get_content()) == payload


def test_a_stalled_transfer_is_abandoned_and_the_link_recovers(tmp_path):
    # A Pi that dies half way through must not wedge the source: without a timeout the state
    # machine sits mid-file for ever and no later capture is ever taken.
    clock = _Clock()
    cable = _Cable()
    ds = _source(tmp_path, cable, clock=clock, stall_timeout=30)
    dead = _Pi(cable, payload=_payload(3000))
    for _ in range(6):
        dead.step()
        ds.check()
    assert not ds.has_pending()

    clock.advance(31_000)
    ds.check()

    revived = _Pi(cable, name="2026-03-09_15-00-00.tar.xz", payload=b"the next capture")
    assert _run(ds, revived)
    assert ds.peek_file().get_name() == "260309-150000.xz"


def test_noise_before_start_is_ignored(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    cable.send(b"boot log line\nanother one\n")
    ds.check()
    pi = _Pi(cable, payload=b"a capture")

    assert _run(ds, pi)
    assert bytes(ds.peek_file().get_content()) == b"a capture"


# -- what the file ends up called -----------------------------------------------------------


def test_a_timestamp_name_keeps_every_digit_and_loses_the_separators(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)

    assert _run(ds, _Pi(cable, name="2024-03-14_12-30-00.tar.xz", payload=b"x"))
    assert ds.peek_file().get_name() == "240314-123000.xz"


def test_a_name_that_is_not_a_timestamp_keeps_its_own(tmp_path):
    # Shortening is positional slicing. Applied to a name that is not a timestamp it produces
    # nonsense, so a producer that names its files anything else keeps the name it chose.
    cable = _Cable()
    ds = _source(tmp_path, cable)

    assert _run(ds, _Pi(cable, name="readings.json", payload=b"x"))
    assert ds.peek_file().get_name() == "readings.json"


def test_the_full_name_is_kept_when_shortening_is_off(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable, shorten_names=False)

    assert _run(ds, _Pi(cable, payload=b"x"))
    assert ds.peek_file().get_name() == PI_NAME


def test_a_wider_name_field_is_read_whole(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable, name_length=40, shorten_names=False)

    assert _run(ds, _Pi(cable, name="a-producer-with-a-longer-name.bin",
                        payload=b"x", name_length=40))
    assert ds.peek_file().get_name() == "a-producer-with-a-longer-name.bin"


def test_two_captures_from_the_same_hour_are_both_kept(tmp_path):
    # The old firmware kept only the hour, so these two collided and one was lost. Shortening
    # now keeps every digit, so they are simply two names.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    assert _run(ds, _Pi(cable, name="2026-03-09_13-25-00.tar.xz", payload=b"first"))
    assert _run(ds, _Pi(cable, name="2026-03-09_13-55-00.tar.xz", payload=b"second"))

    assert _queued(ds) == ["260309-132500.xz", "260309-135500.xz"]


def test_a_capture_is_never_dropped_for_the_sake_of_a_name(tmp_path):
    # The guarantee underneath the naming rule, and it has to hold whatever that rule is: a
    # producer that genuinely repeats a name still gets both files kept. The one already in the
    # queue cannot be replaced, because it may be half way through a transfer.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    assert _run(ds, _Pi(cable, name="readings.json", payload=b"first"))

    second = _Pi(cable, name="readings.json", payload=b"second")
    assert _run(ds, second)
    assert second.confirmed                   # the Pi is released rather than left retrying
    assert _queued(ds) == ["readings-1.json", "readings.json"]

    assert bytes(ds.peek_file().get_content()) == b"first"
    ds.confirm_file()
    assert bytes(ds.peek_file().get_content()) == b"second"


def test_digits_that_are_not_a_date_are_left_alone(tmp_path):
    # Shortening reads the digits of a name as a timestamp, so it checks that they are one.
    cable = _Cable()
    ds = _source(tmp_path, cable)

    assert _run(ds, _Pi(cable, name="batch-1234567890123.bin", payload=b"x"))
    assert ds.peek_file().get_name() == "batch-1234567890123.bin"


def test_a_name_with_a_separator_in_it_is_refused(tmp_path):
    # The name becomes a path here and again on the receiving side. A separator escapes both.
    cable = _Cable()
    ds = _source(tmp_path, cable, shorten_names=False)
    outside = tmp_path / "passwd.tmp"
    outside.write_text("not the queue's to touch")
    pi = _Pi(cable, name="../passwd", payload=b"x")

    _run(ds, pi, rounds=60)
    assert not ds.has_pending()
    # Refused before the arrival recorded the name, so the cleanup that follows a refusal
    # cannot be aimed by it. A name the queue rejects must not reach a delete.
    assert outside.exists()

    # And the link is left usable. The refused producer's last fragment had no line ending, so
    # it swallows the line it is glued to; the transfer after that one is heard normally, which
    # is the resynchronisation `_await_start` documents.
    _run(ds, _Pi(cable, name="2026-03-09_14-00-00.tar.xz", payload=b"lost to the fragment"), 20)
    assert _run(ds, _Pi(cable, payload=b"a capture"))
    assert ds.peek_file().get_name() == PI_NAME       # this source has shortening off
    assert bytes(ds.peek_file().get_content()) == b"a capture"


# -- it is still a durable disk queue -------------------------------------------------------


def test_an_arrived_file_survives_a_reboot_and_leaves_on_confirmation(tmp_path):
    # The reason this subclasses Disk_DataSource rather than DataSource: order, durability
    # and delete-on-confirm come with the queue and are not rebuilt here.
    cable = _Cable()
    ds = _source(tmp_path, cable)
    assert _run(ds, _Pi(cable, payload=b"a capture"))
    assert ds.is_durable()

    rebooted = _source(tmp_path, _Cable())
    assert rebooted.peek_file().get_name() == SHORT_NAME
    assert rebooted.confirm_file().get_name() == SHORT_NAME
    assert not rebooted.has_pending()


def test_files_are_served_in_the_order_they_arrived(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    assert _run(ds, _Pi(cable, name="2026-03-09_13-00-00.tar.xz", payload=b"one"))
    assert _run(ds, _Pi(cable, name="2026-03-09_12-00-00.tar.xz", payload=b"two"))

    assert ds.peek_file().get_name() == "260309-130000.xz"
    ds.confirm_file()
    assert ds.peek_file().get_name() == "260309-120000.xz"


def test_a_delivered_file_can_be_kept(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable, cleanup=False)
    assert _run(ds, _Pi(cable, payload=b"a capture"))
    ds.peek_file()
    ds.confirm_file()

    assert os.listdir(str(tmp_path / "outbox-sent")) == [SHORT_NAME]


# -- the node in front of it ----------------------------------------------------------------


def _make_edge(tmp_path, datasource):
    config_path = str(tmp_path / "edge.json")
    config = {
        "name": "gps",
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


def test_the_edge_serves_what_came_down_the_cable(tmp_path):
    cable = _Cable()
    ds = _source(tmp_path, cable)
    edge = _make_edge(tmp_path, datasource=ds)

    edge._pump_datasource()
    assert edge.file is None                  # nothing has arrived yet

    assert _run(ds, _Pi(cable, payload=_payload(600)))
    edge._pump_datasource()
    assert edge.file.get_name() == SHORT_NAME
    assert bytes(edge.file.get_content()) == _payload(600)

    edge._retire_file(True)
    assert not ds.has_pending()

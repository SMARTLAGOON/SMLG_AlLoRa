"""Serial_DataSource: a producer on the other end of a cable, feeding whole files in.

The deployment is a Raspberry Pi wired to a board over UART. The Pi captures something it
knows how to capture, compresses it, and pushes the archive down the cable; the board holds
the AlLoRa protocol, the outbox and the radio. It is the composition the ubiquitous language
calls board-runs-logic with a serial DataSource, and it is what the GNSS rig has actually been
running since 2024.

**This is per-file movement, not per-packet.** The same physical cable can carry either, and
which one it is depends on where the protocol lives. A Serial *connector* would put the logic
on the Pi and make the board a radio adapter, moving one packet at a time; this moves whole
files and leaves the board in charge. Building the wrong one gives an adapter wearing a
DataSource's name.

**The receive has to be resumable, and that is the whole design.** A capture takes minutes at
9600 baud, and `check()` is called from the serve loop the radio shares, where nothing may
block. So there is no "read a file" call here: there is a state machine that consumes whatever
bytes have arrived, does a bounded amount of work, and returns. It can be stopped between any
two bytes and picked up on the next round. The v2 code this replaces was a single blocking
`while True` with 2000 ms port timeouts inside it, which is why it could only ever run on its
own thread.

**A partial arrival is never a queued file.** The bytes land under a `.tmp`, which the queue's
own rules already exclude from the directory it reads and delete on the next boot, and the
file joins the queue only after its length and checksum both match what was announced. The
failure this rules out is the expensive one: a truncated capture believed to be a short one,
which nothing downstream can tell apart from a real one.

The wire is the Pi's, unchanged, so no Pi in the field has to be touched:

    Pi                                  board
    START\\n                     ->
                                 <-     LISTEN\\n
    <8 byte size>\\n
    <26 byte name>\\n
    <10 byte crc32>\\n           ->
    <crc32 of chunk>\\n <chunk>  ->
                                 <-     ACK\\n   (kept)  or  NACK\\n  (send it again)
    ...
    END\\n                       ->
                                 <-     OK\\n    (the file is on the card; let go of it)

Subclassing Disk_DataSource rather than DataSource is what makes the rest free: send order
across reboots, `is_durable()`, delete only on the peer's confirmation, and the option of
keeping a copy of what was sent. Starting from the base would have rebuilt all of it in RAM,
which is where the readings were being lost in the first place.
"""

import binascii

from AlLoRa.DataSources.Disk_DataSource import Disk_DataSource
from AlLoRa.utils.time_utils import current_time_ms, ticks_diff
from AlLoRa.utils.debug_utils import print

# The header the Pi sends after LISTEN, as fixed-width fields. Reading it as fixed widths
# rather than as lines is deliberate: a length-limited readline returns early on a short field
# and everything after it shifts by a byte, which is silent and produces a plausible-looking
# wrong name. Only the name's width varies between producers, so only that one is a parameter.
_SIZE_FIELD = 8
_CRC_FIELD = 10

_IDLE = 0           # nothing in flight; watching the line for START
_HEADER = 1         # LISTEN sent; waiting for the fixed header block
_CHUNK_HEADER = 2   # between chunks; the next line is a checksum, or END
_CHUNK_BODY = 3     # a checksum arrived; waiting for the bytes it describes

# A line long enough to be noise rather than a message. The longest thing the Pi ever sends on
# its own line is a ten-digit checksum, so anything past this is a producer talking a different
# protocol, or a boot log, and holding it would grow the buffer without bound.
_MAX_LINE = 64

# The digit counts a shortened name accepts: `YYYYMMDDHHMM`, and the same with seconds.
_STAMP_LENGTHS = (12, 14)


class Serial_DataSource(Disk_DataSource):

    def __init__(self, queue_path="Outbox", file_queue_size=25,
                 cleanup=True, archive_path=None, archive_budget=None,
                 uart=None, uart_id=1, baudrate=9600, tx=43, rx=44,
                 link_chunk_size=512, name_length=26, shorten_names=True,
                 stall_timeout=120, clock=None):
        super().__init__(queue_path=queue_path, file_queue_size=file_queue_size,
                         cleanup=cleanup, archive_path=archive_path,
                         archive_budget=archive_budget)
        self.uart_id = uart_id
        self.baudrate = baudrate
        self.tx = tx
        self.rx = rx
        # The cable's chunk, which is not the radio's. The node computes what a LoRa frame can
        # carry and re-clamps it on every RF config change; this is only how much the Pi hands
        # over between acknowledgements, and it has to match what the Pi was written to send.
        self.link_chunk_size = link_chunk_size
        self.name_length = name_length
        self.shorten_names = shorten_names
        # Seconds of silence mid-transfer before the arrival is abandoned. Without it, a Pi
        # that dies half way through leaves the state machine waiting for the rest of a file
        # for ever, and no later capture is ever taken: the link is wedged by a producer that
        # is no longer there.
        self.stall_timeout = stall_timeout
        self._clock = clock if clock is not None else current_time_ms
        self._uart = uart
        self._owns_uart = uart is None
        # Bytes off the port that the state machine has not consumed yet. It holds at most one
        # header or one chunk, because every state drains what it needs the moment it is there.
        self._rx = bytearray()
        # How much is taken off the port per call. The bound is the point: it is what keeps a
        # 200 KB capture already sitting in the driver's buffer from being received inside one
        # `check()`, which is a stretch of seconds where the node cannot hear the radio.
        self._read_budget = 2 * link_chunk_size
        self._reset()
        self._last_progress = self._clock()

    # -- lifecycle -------------------------------------------------------------------------

    def prepare(self):
        if self._uart is None:
            self._uart = self._build_uart()
        # Second, so the queue's own sweep of half-written files runs after the port exists
        # and a producer that starts talking immediately is already being listened to.
        super().prepare()

    def _build_uart(self):
        """The board's port, opened so that a read never waits.

        `timeout=0` is the one line that separates this from the v2 receive. With the 2000 ms
        the old code used, a single read of a chunk that is still in flight parks the node for
        two seconds, and the collector on the other side of the radio times out waiting for a
        packet this node was in no position to answer.

        Not waiting is only half of it: what is not read has to keep. The driver's buffer
        defaults to 256 bytes, which is smaller than one chunk, so a producer that sends 512
        bytes between two rounds overruns it and the middle of the chunk is simply gone. It
        was measured on the bench at 9600 baud with rounds 1500 ms apart: 342 bytes of a
        600-byte burst arrived on the default, the same burst arrived whole at 1024. The size
        asked for here is the one the read budget already uses, so the port holds exactly what
        one round is willing to take, and a chunk with its checksum line in front of it always
        fits inside that.
        """
        import machine
        uart = machine.UART(self.uart_id, baudrate=self.baudrate)
        uart.init(baudrate=self.baudrate, tx=self.tx, rx=self.rx,
                  bits=8, parity=None, stop=1, timeout=0, rxbuf=self._read_budget)
        return uart

    def close(self):
        # The arrival in flight is dropped rather than kept: it is by definition incomplete,
        # and its `.tmp` is swept on the next boot. The queued files stay where they are,
        # which is the whole point of the queue.
        self._abandon(quiet=True)
        if self._owns_uart and self._uart is not None:
            try:
                self._uart.deinit()
            except Exception:
                pass    # not every port has one, and a node shutting down does not care
            self._uart = None
        super().close()

    # -- the pump --------------------------------------------------------------------------

    def check(self):
        """Advance the arrival by whatever has landed, then let the queue do its own round.

        Called from the loop the radio shares, so the work here is bounded twice over: by how
        much is taken off the port, and by the state machine only ever consuming bytes that
        are already in hand. It returns rather than waiting for the rest of anything.
        """
        try:
            self._pump()
        except Exception as e:
            # Nothing a producer sends may take the node down. The arrival is dropped, the
            # buffer is cleared, and the next START is a clean start.
            print("Serial_DataSource: the arrival failed, dropping it:", e)
            self._abandon()
        super().check()

    def _pump(self):
        if self._uart is None:
            return
        if self._fill():
            self._last_progress = self._clock()
        while self._advance():
            self._last_progress = self._clock()
        if self._state != _IDLE and self.stall_timeout:
            silent = ticks_diff(self._clock(), self._last_progress)
            if silent > self.stall_timeout * 1000:
                print("Serial_DataSource: nothing for", self.stall_timeout,
                      "seconds mid-file, abandoning", self._name)
                self._abandon()

    def _fill(self):
        """Take what is waiting on the port, up to this round's budget."""
        budget = self._read_budget
        taken = 0
        while budget > 0:
            waiting = self._uart.any()
            if not waiting:
                break
            data = self._uart.read(min(waiting, budget))
            if not data:
                break
            self._rx.extend(data)
            taken += len(data)
            budget -= len(data)
        return taken > 0

    def _advance(self):
        """One state machine step over the bytes in hand. False when it needs more."""
        if self._state == _IDLE:
            return self._await_start()
        if self._state == _HEADER:
            return self._read_header()
        if self._state == _CHUNK_HEADER:
            return self._read_chunk_header()
        return self._read_chunk_body()

    # -- the states ------------------------------------------------------------------------

    def _await_start(self):
        line = self._take_line()
        if line is None:
            return False
        if line == b"START":
            self._uart.write(b"LISTEN\n")
            self._state = _HEADER
        # Anything else on the line is dropped without comment. A cable shared with a console
        # carries boot messages, and a producer that was mid-file when this node restarted
        # carries the tail of a transfer nobody is receiving. Both resynchronise on the next
        # START, which is the only word this state answers.
        #
        # The match is on the whole line and not on the tail of one, which costs a transfer in
        # one case and is still the right way round. An abandoned arrival can leave a fragment
        # with no line ending on the wire, and the next START glues onto it: `xSTART` is not
        # START, so that transfer goes unanswered and the producer's following one is heard
        # instead. Matching the tail would recover it and would also answer a console line
        # ending in RESTART, which contains the word exactly. A lost transfer is a transfer the
        # producer still holds; a false START is a node reading a boot log as a file header.
        return True

    def _read_header(self):
        block = self._take(_SIZE_FIELD + 1 + self.name_length + 1 + _CRC_FIELD + 1)
        if block is None:
            return False
        at = _SIZE_FIELD + 1
        # Stripped because a fixed-width number can be padded with spaces or with zeros, and
        # which one it is belongs to whoever wrote the producer.
        size = int(block[:_SIZE_FIELD].strip())
        raw_name = block[at:at + self.name_length]
        at += self.name_length + 1
        self._expected_crc = int(block[at:at + _CRC_FIELD].strip())
        if size <= 0:
            # An empty file has no chunks to ask for, so the queue would hold something that
            # can never complete. Refused here, where the producer can still be told why.
            raise ValueError("the producer announced a file of {} bytes".format(size))
        name = self._file_name(raw_name)
        # The writer is opened before the name is kept, so a name the queue refuses is never
        # one this class holds. `_abandon` builds a path out of `_name` to delete the partial
        # file, and a name that got that far while carrying a separator would aim that delete
        # anywhere it liked. What the queue will not accept, the arrival never records.
        writer = self.begin_incoming(name)
        self._name = name
        self._writer = writer
        self._expected_size = size
        self._received = 0
        self._crc = binascii.crc32(b"")
        self._state = _CHUNK_HEADER
        return True

    def _read_chunk_header(self):
        line = self._take_line()
        if line is None:
            return False
        if line == b"END":
            self._finish()
            return True
        # Not END and not a number means the two ends have lost step with each other. There is
        # no way to tell how far, so the arrival goes rather than being guessed at.
        self._chunk_crc = int(line)
        self._chunk_length = min(self.link_chunk_size,
                                 self._expected_size - self._received)
        if self._chunk_length <= 0:
            raise ValueError("the producer sent more than the {} bytes it announced".format(
                self._expected_size))
        self._state = _CHUNK_BODY
        return True

    def _read_chunk_body(self):
        chunk = self._take(self._chunk_length)
        if chunk is None:
            return False
        self._state = _CHUNK_HEADER
        if (binascii.crc32(chunk) & 0xFFFFFFFF) != self._chunk_crc:
            self._uart.write(b"NACK\n")
            # Nothing is written and nothing advances, so the producer's resend of the same
            # chunk is read at the same length and lands in the same place.
            return True
        # Written before it is acknowledged, the other way round from the v2 code. The wire is
        # unchanged either way, but an ACK now means the bytes are on the card rather than that
        # they arrived: a power cut between the two used to lose a chunk the Pi had been told
        # to forget.
        self._writer.write(chunk)
        self._received += len(chunk)
        self._crc = binascii.crc32(chunk, self._crc) & 0xFFFFFFFF
        self._uart.write(b"ACK\n")
        return True

    def _finish(self):
        """END arrived: check what came against what was announced, and queue it or drop it."""
        name = self._name
        self._writer.close()
        self._writer = None
        if self._received != self._expected_size:
            print("Serial_DataSource:", name, "arrived as", self._received,
                  "bytes against the", self._expected_size, "announced, dropping it")
            self._abandon()
            return
        if self._crc != self._expected_crc:
            print("Serial_DataSource:", name, "failed its checksum, dropping it")
            self._abandon()
            return
        queued = self._queue_under(name)
        if queued is None:
            print("Serial_DataSource: no free name for", name, "so it is dropped")
            self.discard_incoming(name)
        else:
            print("Serial_DataSource: queued", queued, self._received, "bytes")
        # Sent whichever way it went, because both outcomes are final: the producer's copy has
        # either been taken or been refused for a reason sending it again will not change, and
        # one left waiting for an answer offers the same capture for ever.
        self._uart.write(b"OK\n")
        self._reset()

    def _queue_under(self, name):
        """Queue the arrival, under a distinct name if that one is taken. The name it got.

        Two captures can want one name: a producer that reuses names, or a timestamp so close
        to the last one that shortening lands them together. The file already in the queue
        cannot be replaced, because it may be half way through a transfer whose remaining
        chunks would then come out of a different file. So the new one is queued beside it
        rather than dropped: a capture that reached the card is never thrown away for the sake
        of a name.

        The search is bounded by the queue's own size, which is the most names that can be
        taken at once, so it ends whatever the producer does.
        """
        if self.commit_incoming(name):
            return name
        stem, extension = self._split_extension(name)
        for suffix in range(1, self.file_queue_size + 1):
            candidate = "{}-{}{}".format(stem, suffix, extension)
            if self.commit_incoming(name, queue_as=candidate):
                return candidate
        return None

    # -- names -----------------------------------------------------------------------------

    def _file_name(self, raw):
        """The name this file is queued, sent and reassembled under.

        The field is fixed width, so the padding a producer used to fill it is not part of the
        name. What is left is either shortened or taken as it stands.
        """
        name = raw.decode("utf-8").strip().strip("\x00").strip()
        if self.shorten_names:
            return self._shorten(name)
        return name

    def _shorten(self, name):
        """`2026-03-09_13-25-00.tar.xz` -> `260309-132500.xz`: every digit, no separators.

        The rig's old firmware cut the same name down to `26030913.xz`, keeping the hour and
        throwing the minutes and seconds away, so two captures from one hour arrived under one
        name and one of them had to lose. Both reasons for paying that have since failed. The
        card is not an 8.3 filesystem: the rig's own firmware created `outbox_files/` on it, a
        twelve-character name no such filesystem can hold. And the name is not expensive on the
        air: v3 carries 255 bytes in a frame at every spreading factor, so the ten bytes this
        keeps are about four percent of the one packet that ever carries them, once per file.

        What is left is worth having. Half the length of the producer's name, still sorts
        chronologically, still reads as a date at a glance, and distinguishes to the second.

        Only a name that really is a timestamp is touched. Every other one is kept exactly as
        the producer sent it, because a rule that rewrites names it does not understand
        produces a plausible-looking name built from the wrong characters, which is worse than
        a long one.
        """
        stem, extension = self._split_extension(name)
        digits = ""
        for character in stem:
            if "0" <= character <= "9":
                digits += character
        if len(digits) not in _STAMP_LENGTHS or not self._is_timestamp(digits):
            return name
        return digits[2:8] + "-" + digits[8:] + extension

    def _is_timestamp(self, digits):
        """Whether the digits of a name read as a date and time this century.

        Checked rather than assumed, so a producer numbering its files `batch-20240001.bin`
        gets its own name back instead of a date that never happened.
        """
        year, month, day, hour = digits[0:4], digits[4:6], digits[6:8], digits[8:10]
        return ("2000" <= year <= "2099" and "01" <= month <= "12"
                and "01" <= day <= "31" and hour <= "23")

    def _split_extension(self, name):
        """`a.tar.xz` -> (`a.tar`, `.xz`). The last extension only, which is the real one."""
        dot = name.rfind(".")
        if dot > 0:
            return name[:dot], name[dot:]
        return name, ""

    # -- the buffer ------------------------------------------------------------------------

    # The tail is rebuilt rather than trimmed in place, here and below. MicroPython's bytearray
    # has no slice deletion: `del buf[:n]` raises TypeError on a board and is accepted by
    # CPython, so it passes every test off the device and then fails on the first byte that
    # reaches a real one. `Serial_link._read_frame` was caught by exactly this.

    def _take_line(self):
        """The next completed line, without its terminator. None if it has not all arrived."""
        at = self._rx.find(b"\n")
        if at < 0:
            if len(self._rx) > _MAX_LINE:
                # No line ending in sight and more bytes than any message this protocol has.
                # Dropped rather than held, so a producer talking something else cannot grow
                # the buffer until the heap gives out.
                self._rx = bytearray(self._rx[-_MAX_LINE:])
            return None
        line = bytes(self._rx[:at])
        self._rx = bytearray(self._rx[at + 1:])
        return line.strip()

    def _take(self, count):
        """Exactly `count` bytes, or None until they are all in hand."""
        if len(self._rx) < count:
            return None
        data = bytes(self._rx[:count])
        self._rx = bytearray(self._rx[count:])
        return data

    # -- giving up -------------------------------------------------------------------------

    def _abandon(self, quiet=False):
        """Drop the arrival in flight and go back to listening for the next START.

        The partial file goes with it. It is the one thing that must not survive: left on the
        card under its real name it would be adopted by the queue's reconcile and sent as a
        whole capture, and a truncated reading is indistinguishable from a short one anywhere
        downstream.
        """
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._name is not None:
            self.discard_incoming(self._name)
        if not quiet and self._name is not None:
            print("Serial_DataSource: dropped the partial arrival", self._name)
        # The buffer goes too: whatever is in it belongs to the transfer that just failed, and
        # reading it as the next one is how two files become one.
        self._rx = bytearray()
        self._reset()

    def _reset(self):
        self._state = _IDLE
        self._writer = None
        self._name = None
        self._expected_size = 0
        self._expected_crc = 0
        self._received = 0
        self._crc = 0
        self._chunk_crc = 0
        self._chunk_length = 0

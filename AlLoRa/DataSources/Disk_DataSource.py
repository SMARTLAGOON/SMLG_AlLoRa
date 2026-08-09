"""Disk_DataSource: the outbound queue that survives losing power.

The mirror of Disk_DataSink. That sink takes each file the collector role finished
receiving and writes it under a folder; this source takes each file sitting in a folder
and hands it to the source role to send. One is where files land, the other is where
they wait, and between them a deployment can be restarted at any point without a file
falling through the gap.

Which is the whole reason it exists. The queue on the DataSource base lives in RAM, so a
node that lost power between reading a sensor and getting the reading on the air lost the
reading, silently and with nothing to retry. A field node that only reports every few
hours can lose most of a day that way, and nothing downstream can tell the difference
between a node that had nothing to say and a node whose readings never survived to be
said. Here the file is on flash before it is ever queued, and it is deleted only once the
far end confirms it: the node may repeat itself after a reboot, but it does not go quiet.

**Two sources of truth, deliberately, and neither can lose a file.**

- The *directory* decides what is pending. A file in it is queued, its absence is delivery.
- A small index beside it, `queue.json`, decides what order they go in. It holds names, not
  contents.

They are reconciled rather than trusted. A file the index has never heard of is adopted
onto the end of the queue, so a producer can still drop a file into the folder by hand and
have it sent. A name in the index with no file behind it is forgotten. The failure mode of
the index is therefore a file going out in the wrong *order*, never a file going missing,
and that is the reason for the split: a directory cannot remember the order things were put
into it. `listdir` returns entries in whatever arrangement the filesystem is holding, which
is not a promise on either filesystem an ESP32 might be flashed with, and on FAT it visibly
changes as files are deleted and their slots reused. Deleting a file on every delivery is
what this class does all day, so that is the normal case and not an edge one. File
timestamps are no help either: board clocks come up unset.

Writes are ordered so that an interruption costs order and never data. The payload is
committed first and the index second, because a crash between the two leaves a file that
the next reconcile adopts. Doing it the other way round would leave the index describing a
file that was never written.

MicroPython target discipline: the payload is read from flash only when the node is
actually about to send it, the directory is re-scanned only when the queue has run dry
rather than on every radio round, both writes go through the same all-or-nothing commit the
config files use, and nothing here opens more than one file at a time.
"""

from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.File import AlLoRa_File
from AlLoRa.utils.file_utils import commit_bytes, commit_file
from AlLoRa.utils.json_utils import json
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.debug_utils import print

INDEX_NAME = "queue.json"


class Disk_DataSource(DataSource):

    def __init__(self, file_chunk_size, queue_path="Outbox", file_queue_size=25):
        super().__init__(file_chunk_size, file_queue_size=file_queue_size)
        self.queue_path = queue_path
        # The send order, by name. The directory says what exists; this says in what
        # order, and is reconciled against the directory rather than believed.
        self._order = []
        # The head, built once and kept until it is confirmed or the queue moves on.
        self._head_name = None
        self._head_file = None

    # -- lifecycle -------------------------------------------------------------------------

    def prepare(self):
        """Create the folder, clear any temp left by a write that was cut off, and rebuild
        the order from whatever is actually on the card.

        A `.tmp` file is by definition a payload nobody finished writing, so it is not a
        partial reading to be rescued: the commit that would have named it never ran.
        Leaving them costs the one thing an embedded filesystem cannot spare, and they
        accumulate one per power cut forever.
        """
        try:
            os.mkdir(self.queue_path)
        except OSError:
            pass    # already there, which is the normal case after the first boot
        for name in self._listdir():
            if name.endswith(".tmp"):
                self._remove(name)
        self._order = self._load_index()
        self._reconcile()

    def check(self):
        # The non-blocking pump the serve loop calls every round, so it re-scans only when
        # there is nothing left to send. While the queue has work the directory cannot tell
        # it anything it does not already know, and a flash read per radio round to learn
        # that is a read not worth doing. A file dropped in by hand during a backlog is
        # picked up when the backlog clears, which is where it would have been sent anyway.
        if not self._order:
            self._reconcile()

    def close(self):
        # Only the in-RAM copy of the head. The file itself stays queued on flash, which
        # is the point: shutting a node down is not delivering its backlog.
        self._head_name = None
        self._head_file = None

    # -- the queue -------------------------------------------------------------------------

    def enqueue(self, name, payload):
        """Put a payload on the back of the queue durably, and return whether it was taken.

        The payload is written under a temporary name and renamed into place, so a file that
        appears in the folder is always a whole one: a reader finding it mid-write would
        otherwise send a truncated reading that looks exactly like a short one.
        """
        if not name or "/" in name:
            # The name doubles as a filename here and again on the receiving side, where it
            # builds the reassembly path. A separator in it escapes both.
            raise ValueError("queued file name must be a plain name, got {}".format(name))
        if name == INDEX_NAME:
            raise ValueError("{} is the queue's own index".format(INDEX_NAME))
        if not payload:
            # An empty file has no chunks to ask for, so it would sit at the head of the
            # queue and never complete. Refused at the door, where the producer can still
            # see why, rather than becoming a stall nobody can explain later.
            print("Disk_DataSource: refusing to queue an empty file", name)
            return False
        if name in self._order:
            # Same rule as the base queue: a repeated name is dropped rather than
            # overwriting a file that may be mid-delivery.
            return False
        self._make_room()
        commit_bytes(self.queue_path + "/" + name, payload)
        self._order.append(name)
        # Second, always: interrupted here, the file is on the card without a place in the
        # order, and the next reconcile adopts it. Interrupted the other way round, the
        # order would name a payload that does not exist.
        self._save_index()
        return True

    def add_to_queue(self, file: AlLoRa_File):
        """The base's verb, routed to flash: queue this file's bytes under its own name."""
        content = file.get_content()
        self.enqueue(file.get_name(), bytes(content) if content else b"")

    def has_pending(self):
        return bool(self._order)

    def peek_file(self):
        """The file at the front as an AlLoRa_File, without removing it. None if empty."""
        while self._order:
            name = self._order[0]
            if name == self._head_name and self._head_file is not None:
                return self._head_file
            payload = self._read(name)
            if not payload:
                # Unreadable or empty rather than absent: a card pulled mid-run, a corrupt
                # entry, a zero-length file dropped in by hand. There is nothing to send,
                # and left in place it would hold the front of the queue against every file
                # behind it. It goes, loudly, and the next one is tried.
                print("Disk_DataSource: dropping unusable queued file", name)
                self._forget(name)
                continue
            self._head_name = name
            self._head_file = AlLoRa_File(name=name, content=bytearray(payload),
                                          chunk_size=self.file_chunk_size)
            return self._head_file
        self.close()
        return None

    def confirm_file(self):
        """Delivered: erase the borrowed file and return it. None if there was none.

        The only place a file leaves the queue on a successful path, and it runs on the
        peer's confirmation rather than on this node's decision to stop trying. It erases
        the file that was peeked by name, not whatever is at the front now: a queue that
        filled up mid-delivery may have evicted the file being served, and going by position
        would delete the one that replaced it, which has not been sent.
        """
        name = self._head_name
        delivered = self._head_file
        self._head_name = None
        self._head_file = None
        if name is None:
            # Confirmed without a peek. The base drains its head in that case, so this does
            # too rather than diverging on a shared contract.
            if self._order:
                self._forget(self._order[0])
            return None
        self._forget(name)
        return delivered

    def get_next_file(self):
        """The v2 destructive pop, kept working for subclasses written against it.

        It hands the file over and erases it in one step, so a caller taking this path gets
        the old at-most-once behavior and none of the durability. The node's serve loop does
        not use it.
        """
        file = self.peek_file()
        if file is None:
            return None
        self.confirm_file()
        return file

    # -- internals -------------------------------------------------------------------------

    def _make_room(self):
        """Evict from the front until there is space, never the file being delivered.

        Said out loud, because on a durable queue this is a reading being destroyed, and a
        queue quietly eating its own backlog looks identical to a link that is keeping up.
        """
        while len(self._order) >= self.file_queue_size:
            victim = None
            for name in self._order:
                if name != self._head_name:
                    victim = name
                    break
            if victim is None:
                return      # nothing evictable: the only entry is on the air right now
            print("Disk_DataSource: queue full, dropping oldest file", victim)
            self._forget(victim, save=False)
        return

    def _forget(self, name, save=True):
        """Drop a file from both truths, the card first."""
        self._remove(name)
        try:
            self._order.remove(name)
        except ValueError:
            pass
        if name == self._head_name:
            self._head_name = None
            self._head_file = None
        if save:
            self._save_index()

    def _reconcile(self):
        """Make the order agree with the card: forget what is gone, adopt what is new."""
        on_disk = self._names_on_disk()
        order = [n for n in self._order if n in on_disk]
        known = set(order)
        # Anything the index never knew about goes on the end, in name order among
        # themselves, so a hand-dropped file is sent rather than ignored.
        order.extend(sorted(n for n in on_disk if n not in known))
        if order != self._order:
            self._order = order
            self._save_index()

    def _load_index(self):
        try:
            with open(self.queue_path + "/" + INDEX_NAME, "r") as f:
                order = json.loads(f.read()).get("order", [])
        except (OSError, ValueError):
            # Missing on a first boot, and unreadable if a card went bad. Either way the
            # directory still holds every queued file, so the cost is the order, not the data.
            return []
        return [n for n in order if isinstance(n, str)]

    def _save_index(self):
        try:
            commit_file(self.queue_path + "/" + INDEX_NAME,
                        json.dumps({"order": self._order}))
        except OSError as e:
            # A full or absent card. The files themselves are what matter and they are
            # already written; on the next boot they are adopted in name order.
            print("Disk_DataSource: could not write the queue order:", e)

    def _listdir(self):
        try:
            return os.listdir(self.queue_path)
        except OSError:
            return []    # not created yet: an empty queue, not an error

    def _names_on_disk(self):
        return set(n for n in self._listdir()
                   if n != INDEX_NAME and not n.startswith(".") and not n.endswith(".tmp"))

    def _read(self, name):
        try:
            with open(self.queue_path + "/" + name, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _remove(self, name):
        try:
            os.remove(self.queue_path + "/" + name)
        except OSError:
            pass

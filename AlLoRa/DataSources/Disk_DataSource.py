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

MicroPython target discipline: a payload is never held in RAM at all, only the chunk the
radio is asking for right now; the directory is re-scanned only when the queue has run dry
rather than on every radio round; both writes go through the same all-or-nothing commit the
config files use; and nothing here opens more than one file at a time.

That first point costs one open handle, held for as long as a file is at the head of the
queue and closed on every path it can leave by. The alternative was reading the whole
payload and handing it to a file object that kept it too: two resident copies, a ceiling on
what a node could send that had nothing to do with the protocol, and a stretch of seconds
where a node loading a file could not hear the radio.
"""

from AlLoRa.DataSources.DataSource import DataSource
from AlLoRa.File import AlLoRa_File, OnDemandFileReader
from AlLoRa.utils.file_utils import commit_bytes, commit_file
from AlLoRa.utils.json_utils import json
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.debug_utils import print

INDEX_NAME = "queue.json"

# What the archive is allowed to hold when the filesystem will not say how big it is, and
# the ceiling is deliberately low. A board whose `statvfs` is missing could be anything,
# including the 2 MB internal partition, and an archive is a convenience while the outbox is
# the job: crowding out a reading that has not been sent yet to keep one that has would
# invert the whole point of the queue. An operator with a card under it passes
# `archive_budget` and gets whatever they ask for.
ARCHIVE_BUDGET_FALLBACK = 256 * 1024

# The share of the volume the archive may take when it sizes itself. Read from the total
# rather than from what is free right now, so the budget is the same number on every boot
# instead of shrinking towards nothing as the archive itself fills the card.
ARCHIVE_BUDGET_SHARE = 4       # a quarter


class Disk_DataSource(DataSource):

    def __init__(self, queue_path="Outbox", file_queue_size=25,
                 cleanup=True, archive_path=None, archive_budget=None):
        super().__init__(file_queue_size=file_queue_size)
        self.queue_path = queue_path
        # Erase a file once the peer confirms it, or keep a copy. True is what this class has
        # always done and stays the default, so every config already in the field is
        # unchanged. False buys the one thing delivery-is-deletion cannot give: something on
        # the card to compare against what arrived, when a transfer succeeded and the reading
        # in it was still wrong.
        #
        # The same word as `cleanup` on the sinks, meaning the same thing on the other side
        # of the boundary layer: discard the payload once it is safely somewhere else.
        self.cleanup = cleanup
        # A sibling of the outbox and never a child of it. `_names_on_disk` filters the index,
        # dotfiles and temps and nothing else, so a directory inside `queue_path` would be
        # adopted as a queued file and then fail to open, every round, forever.
        self.archive_path = archive_path if archive_path else queue_path + "-sent"
        self.archive_budget = archive_budget
        # Delivered names in the order they were archived, and the bytes they occupy. Both
        # are rebuilt at prepare() and maintained from there rather than re-scanned, because
        # a directory listing per delivery is a flash read the radio loop pays for.
        self._archived = []
        self._archive_bytes = 0
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
        if not self.cleanup:
            self._prepare_archive()

    def _prepare_archive(self):
        """Open the archive and work out what it already holds.

        The order it comes back in is whatever the filesystem reports, which is not the order
        the files were delivered in: `listdir` makes no promise, and the timestamps that would
        settle it are useless on a board whose clock comes up unset. It decides only which
        copy is dropped first when the budget is reached, so the cost of getting it wrong is
        keeping a slightly different set of old files than intended. The queue itself keeps a
        real index because there the same mistake would send readings out of order.
        """
        try:
            os.mkdir(self.archive_path)
        except OSError:
            pass    # already there, which is the normal case after the first boot
        self._archived = []
        self._archive_bytes = 0
        for name in self._archive_listdir():
            if name.endswith(".tmp"):
                self._archive_remove(name)
                continue
            self._archived.append(name)
            self._archive_bytes += self._archive_size(name)
        if self.archive_budget is None:
            self.archive_budget = self._default_archive_budget()

    def _default_archive_budget(self):
        """A share of the volume, or a low fixed ceiling when it will not say.

        A constant here would be wrong on both boards this runs on at once: generous enough
        for an SD card is most of the internal flash, and safe for the internal flash throws
        away almost all of a card an operator put in for exactly this.
        """
        try:
            stats = os.statvfs(self.archive_path)
            total = stats[1] * stats[2]     # f_frsize * f_blocks
            if total > 0:
                return total // ARCHIVE_BUDGET_SHARE
        except (AttributeError, OSError, IndexError):
            pass    # no statvfs on this port, or the path is not on a mounted volume
        print("Disk_DataSource: the filesystem would not report its size, so the archive is",
              "capped at", ARCHIVE_BUDGET_FALLBACK, "bytes")
        return ARCHIVE_BUDGET_FALLBACK

    def check(self):
        # The non-blocking pump the serve loop calls every round, so it re-scans only when
        # there is nothing left to send. While the queue has work the directory cannot tell
        # it anything it does not already know, and a flash read per radio round to learn
        # that is a read not worth doing. A file dropped in by hand during a backlog is
        # picked up when the backlog clears, which is where it would have been sent anyway.
        if not self._order:
            self._reconcile()

    def close(self):
        # Only the head's handle. The file itself stays queued on flash, which is the
        # point: shutting a node down is not delivering its backlog.
        self._release_head()

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

    def is_durable(self):
        """Yes, and what that promises is narrower than the word suggests: **the same name
        in this queue is the same bytes**.

        It holds because `enqueue` refuses a name already queued and a file leaves only on
        the peer's confirmation, so a name still at the front after a reboot belongs to the
        file that was being sent, with its contents untouched. That is exactly what the
        serve loop needs in order to carry on from the collector's next missing index
        instead of re-sending an hour of chunks.

        The known hole is a file dropped into the folder by hand, adopted by the reconcile,
        and then replaced under the same name across a restart. That is a documented limit
        of hand-dropped files rather than something this queue can check.
        """
        return True

    def peek_file(self):
        """The file at the front as an AlLoRa_File, without removing it. None if empty.

        The payload is not read here. What comes back is positioned on the file and reads
        each chunk as the radio asks for it, so a node serving a file holds a handle and a
        few hundred bytes rather than two copies of the artifact. That is what lifts the
        ceiling on what a node can send and keeps it listening while it serves.
        """
        while self._order:
            name = self._order[0]
            if name == self._head_name and self._head_file is not None:
                return self._head_file
            reader = self._open(name)
            if reader is None or len(reader) == 0:
                # Unreadable or empty rather than absent: a card pulled mid-run, a corrupt
                # entry, a zero-length file dropped in by hand. There is nothing to send,
                # and left in place it would hold the front of the queue against every file
                # behind it. It goes, loudly, and the next one is tried.
                #
                # The emptiness check is load-bearing beyond the stall it prevents:
                # AlLoRa_File branches on the truthiness of its content, and an empty
                # reader is falsy, so an empty file would come back as a *receiving* file
                # and start laying down Temp folders on the sending side.
                if reader is not None:
                    reader.close()
                print("Disk_DataSource: dropping unusable queued file", name)
                self._forget(name)
                continue
            self._head_name = name
            # No chunk size: the folder knows the bytes and not the radio. The node stamps
            # its clamped value each time it installs this file, so an attempt made after a
            # retune is cut for the config it will actually go out on.
            self._head_file = AlLoRa_File(name=name, content=reader)
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
        # The handle goes before the file does, and what is handed back is a receipt: its
        # name and its delivery counters. Its bytes are on a file this call erases.
        self._release_head()
        if name is None:
            # Confirmed without a peek. The base drains its head in that case, so this does
            # too rather than diverging on a shared contract.
            if self._order:
                self._forget(self._order[0], delivered=True)
            return None
        self._forget(name, delivered=True)
        return delivered

    def get_next_file(self):
        """The v2 destructive pop, kept working for subclasses written against it.

        It hands the file over and erases it in one step, so a caller taking this path gets
        the old at-most-once behavior and none of the durability. The node's serve loop does
        not use it.

        This is the one place the payload is still read whole. Handing back a file
        positioned on flash and then deleting that flash would give the caller a reader over
        bytes that are gone, so the contract this method exists to honour ("take it, it is
        yours") is kept by materialising it. A v2 subclass calling this gets what it always
        got, and pays what it always paid.
        """
        file = self.peek_file()
        if file is None:
            return None
        name = file.get_name()
        payload = self._read(name)
        self.confirm_file()
        if not payload:
            # It was there for the peek and gone for the read. Nothing to hand over, and an
            # empty content would come back as a *receiving* file rather than as no file.
            print("Disk_DataSource: queued file vanished before it could be taken", name)
            return None
        return AlLoRa_File(name=name, content=bytearray(payload))

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

    def _forget(self, name, save=True, delivered=False):
        """Drop a file from both truths, the card first.

        The handle comes before either, when this is the file being served: an open reader
        over a file that has just been deleted is defined behaviour on POSIX and not on
        littlefs or FAT, and the node runs on those.

        `delivered` says the peer confirmed this file, which is the only way out of the queue
        that earns a copy. Eviction under a full queue comes through here too and must not:
        that path is a reading being destroyed *because* there was no room, and moving it
        sideways into another folder on the same card would not make room at all.
        """
        if name == self._head_name:
            self._release_head()
        if delivered and not self.cleanup:
            self._archive(name)
        else:
            self._remove(name)
        try:
            self._order.remove(name)
        except ValueError:
            pass
        if save:
            self._save_index()

    def _release_head(self):
        """Let go of the head: close its handle and forget it. The queue entry stays."""
        if self._head_file is not None:
            self._head_file.release()
        self._head_name = None
        self._head_file = None

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

    def _open(self, name):
        """A reader positioned on a queued file, or None if it is not there to open."""
        try:
            return OnDemandFileReader(self.queue_path + "/" + name)
        except OSError:
            return None

    def _read(self, name):
        """The whole payload. Only the legacy destructive pop still wants this."""
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

    # -- the archive -----------------------------------------------------------------------

    def _archive(self, name):
        """Move a delivered file out of the queue, or erase it if it cannot be moved.

        The one hard rule is that the file does not stay where it is. `_reconcile` adopts
        anything in the queue folder the index does not know, which is what lets a producer
        drop a file in by hand, and it cannot tell that apart from a delivered file left
        behind: the reading would be sent again on the next drain, and again after that.
        So every failure here falls through to the delete this class would have done anyway.
        A lost copy is the feature not working; a file left in the outbox is the node stuck.
        """
        size = self._size(name)
        if name in self._archived:
            # A repeat of a name already kept, which is ordinary: a producer that names its
            # files by the hour reuses one every time the hour comes round. The old copy's
            # bytes come off the books here rather than when it is overwritten, because on
            # littlefs and POSIX the rename below replaces it without a word, and nothing
            # else would ever subtract them: the budget would drift upwards with every
            # repeat until the archive was effectively uncapped.
            self._archive_forget(name)
        self._make_archive_room(size)
        source = self.queue_path + "/" + name
        target = self.archive_path + "/" + name
        try:
            try:
                os.rename(source, target)
            except OSError:
                # Same split as the config commit: littlefs replaces the target, FAT refuses
                # a name already in use, and an ESP32 can be flashed either way. A repeat of
                # a name already archived is the ordinary way to get here.
                self._archive_remove(name)
                os.rename(source, target)
        except OSError as e:
            print("Disk_DataSource: could not archive a delivered file, erasing it:", name, e)
            self._remove(name)
            return
        self._archived.append(name)
        self._archive_bytes += size

    def _make_archive_room(self, incoming):
        """Evict oldest-first until the incoming file fits under the budget.

        Said out loud for the same reason `_make_room` says it: a store quietly eating itself
        to stay under a limit looks exactly like one that has plenty of space.
        """
        while self._archived and self._archive_bytes + incoming > self.archive_budget:
            victim = self._archived[0]
            print("Disk_DataSource: archive full, dropping oldest delivered file", victim)
            self._archive_forget(victim)

    def _archive_forget(self, name):
        self._archive_bytes -= self._archive_size(name)
        if self._archive_bytes < 0:
            self._archive_bytes = 0
        self._archive_remove(name)
        try:
            self._archived.remove(name)
        except ValueError:
            pass

    def _archive_listdir(self):
        try:
            return os.listdir(self.archive_path)
        except OSError:
            return []

    def _archive_remove(self, name):
        try:
            os.remove(self.archive_path + "/" + name)
        except OSError:
            pass

    def _size(self, name):
        return self._stat_size(self.queue_path + "/" + name)

    def _archive_size(self, name):
        return self._stat_size(self.archive_path + "/" + name)

    def _stat_size(self, path):
        try:
            return os.stat(path)[6]
        except (OSError, IndexError):
            return 0

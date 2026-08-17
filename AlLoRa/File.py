import gc
from math import ceil
from AlLoRa.utils.debug_utils import print
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.time_utils import current_time_ms as time

# try:
#     from utime import ticks_ms as time
#     import uos as os
# except:
#     from time import time
#     import os
gc.enable()

class OnDemandFileWriter:
    def __init__(self, filename):
        try:
            # 'wb+' (not 'wb') so chunks can be placed at arbitrary offsets via seek().
            self.file = open(filename, 'wb+')
        except Exception as e:
            print("Error opening file: ", filename, ": ", e)

    def seek(self, position):
        self.file.seek(position)

    def write(self, data):
        self.file.write(data)

    def flush(self):
        self.file.flush()

    def close(self):
        self.file.close()

class AlLoRa_File:

    def __init__(self, name: str = None, content: bytearray = None, chunk_size: int = None, length: int = None, total_len: int = None, report=False, path="Results"):
        self.name = name
        self.report = report
        if content:
            self.assembly_needed = False
            self.content = content
            self.length = len(content)
            # How this file is cut is decided by whatever will carry it, and that may not
            # exist yet: a queue on flash builds the file from bytes it was handed, and a
            # broker message becomes a file on the network thread that delivered it. Both
            # happen before any radio is consulted. So None is a legitimate answer here and
            # means undecided, not zero: the node stamps its own clamped size when it
            # installs the file to serve, which is the only place that knows the posture the
            # frame will travel in.
            self.chunk_size = chunk_size
            if chunk_size is None:
                self.chunk_counter = None
            else:
                self.chunk_counter = ceil(self.length / chunk_size)

            self.retransmission = 0
            self.last_chunk_sent = None

            self.sent = False
            self.metadata_sent = False
            self.first_sent = None
            self.last_sent = None
        else:
            self.assembly_needed = True
            self.length = length
            self.chunk_counter = length
            # The receiver needs the sender's chunk_size to place each chunk at its
            # absolute offset (positioned writes). In a matched deployment this is the
            # node's own configured chunk_size, threaded in by the Collector.
            self.chunk_size = chunk_size
            # The sender's exact byte count, when it advertised one. `length` above is a
            # chunk *count*, and counting is what let a peer splice two files together: a
            # full-size chunk standing in for the real short tail leaves no index missing,
            # so the file looked complete at the wrong size. The byte total fixes every
            # chunk's expected length in advance, which is what makes a wrong one visible.
            # None on a v2 link, whose METADATA carries no byte count at all.
            self.total_len = total_len
            self.path = path
            # Check if Temp folder exists
            try:
                os.mkdir(self.path)
            except Exception as e:
                print("Error creating base folder: ", e)
            try:
                os.mkdir("{}/Temp".format(self.path))
            except Exception as e:
                print("Error creating Temp folder: ", e)
            self.temp_file_path = "{}/Temp/{}.tmp".format(self.path, name)
            self.file_writer = OnDemandFileWriter(self.temp_file_path)
            self.received_chunks = 0
            self.missing_chunks = list(range(length))

    def get_name(self):
        return self.name

    def get_content(self):
        if self.assembly_needed:
            # Flush positioned writes still buffered in the open writer before the
            # separate read handle reads them back (no-op once finalize() closed it).
            try:
                self.file_writer.flush()
            except Exception:
                pass
            with open(self.temp_file_path, "rb") as f:
                self.content = f.read()
            return self.content
        return self.content

    # collector-side methods
    def get_missing_chunks(self) -> list:
        return self.missing_chunks

    def expected_chunk_len(self, order: int):
        # What chunk `order` must weigh, derived from the advertised byte total: every
        # chunk is full except the last, which carries the remainder. Keyed on the index
        # and not on how many chunks have landed, so it holds for a burst arriving out of
        # order exactly as it does for the in-order case. None means unanswerable, and
        # therefore unenforceable: no advertised total (a v2 peer), or an index outside
        # the file.
        if self.total_len is None or not self.chunk_size:
            return None
        if order < 0 or order >= self.chunk_counter:
            return None
        last = self.chunk_counter - 1
        if order < last:
            return self.chunk_size
        return self.total_len - last * self.chunk_size

    def add_chunk(self, order: int, chunk: bytes):
        # Positioned + idempotent: place the chunk at its absolute offset, so
        # out-of-order arrival reassembles correctly and a duplicate just
        # overwrites the same bytes (no append, no double-count). v2 appended in
        # arrival order and ignored `order` -- the silent-corruption bug.
        #
        # Returns True when the chunk was placed, False when it was refused. A refusal
        # means the answer disagrees with what the sender advertised for this file, which
        # is not damage in transit (the frame already passed its integrity check) but a
        # peer serving something else: it rebooted, or its file was swapped mid-transfer.
        # Refuse before writing, so the reassembly keeps only bytes that belong to it.
        expected = self.expected_chunk_len(order)
        if expected is not None and len(chunk) != expected:
            print("Refusing chunk {} of {}: {} bytes where {} were advertised".format(
                order, self.name, len(chunk), expected))
            return False
        try:
            self.file_writer.seek(order * self.chunk_size)
            self.file_writer.write(chunk)
            if order in self.missing_chunks:
                self.missing_chunks.remove(order)
                self.received_chunks += 1
            return True
        except Exception as e:
            print("Error adding chunk: ", e)
            return False

    def finalize(self, path=None):
        self.file_writer.close()
        if not path:
            path = self.path
        print("Trying to save file: ", self.temp_file_path + " -> " + path + "/" + self.name)
        os.rename(self.temp_file_path, path + "/" + self.name)

    def save(self, path=None):
        if path:
            try:
                os.mkdir(path)
            except:
                pass
        self.finalize(path)

    def discard(self):
        # save()'s counterpart for a non-disk sink (MQTT/cloud) that has already read
        # get_content(): close the reassembly writer and drop the temp file instead of
        # renaming it into place, so no Results copy is left and no file handle leaks.
        if not self.assembly_needed:
            return
        try:
            self.file_writer.close()
        except Exception:
            pass
        try:
            os.remove(self.temp_file_path)
        except Exception:
            pass

    # source-side methods
    def reset_delivery(self):
        # One file object can be served more than once, re-queued after delivery,
        # or broadcast to several endpoints. Every new serve must start from an
        # undelivered state, or the stale sent flag confirms a delivery that never
        # happened. Receiver-side (reassembly) files have no delivery state.
        if self.assembly_needed:
            return
        self.sent = False
        self.metadata_sent = False
        self.first_sent = None
        self.last_sent = None
        self.last_chunk_sent = None
        self.retransmission = 0

    def get_length(self):
        return self.chunk_counter

    def change_chunk_size(self, new_size):
        # Source-side only. On a reassembly file `length` is a chunk *count* and every chunk
        # already written sits at an offset derived from the sender's announced size, so
        # re-cutting one would renumber bytes that are already on the card.
        if self.assembly_needed:
            raise ValueError(
                "cannot re-cut {}: it is being reassembled, and its chunk size is the "
                "sender's announced one".format(self.name))
        self.chunk_size = new_size
        self.chunk_counter = ceil(self.length / self.chunk_size)

    def sent_ok(self):
        self.report_SST(False)
        self.sent = True

    def get_chunk(self, position: int):
        if self.last_chunk_sent:
            self.check_retransmission(position)
        self.last_chunk_sent = position
        return bytes(self.content[position * self.chunk_size: position * self.chunk_size + self.chunk_size])

    def check_retransmission(self, requested_chunk):
        if requested_chunk == self.last_chunk_sent:
            self.retransmission += 1
            return True
        return False

    def report_SST(self, t0_tf, report=False):
        t = time() / 1000
        if t0_tf:
            self.first_sent = t
        elif self.first_sent is not None:
            self.last_sent = t
            txt = "{} -> size: {} (chunks:{}) ;t0: {}; tf {}; SST: {}; Retransmission: {}\n".format(
                self.get_name(), self.length, self.chunk_counter, self.first_sent, t, t - self.first_sent,
                self.retransmission)
            print(txt)
            if report and self.report:
                with open('log.txt', "ab") as test_log:
                    test_log.write(txt.encode())

if __name__ == "__main__":
    x = AlLoRa_File(name="Test", length=100)
    print(x.get_name())
    y = AlLoRa_File(name="Test2", content=bytearray(b"1111111111111111111"), chunk_size=2)
    print(y.get_name())
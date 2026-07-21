"""DataSource, the node's input boundary: whatever feeds AlLoRa_Files to the serve side.

The mirror of DataSinks: a DataSource queues files *in* for the node currently in the
source role, exactly as a DataSink drains completed files *out* of the collector role.
The polling-thread API (start/read/stop) is the legacy way to drive one; it still works,
but nothing here requires a thread. The queue can be fed from any loop.

Runtime-agnostic on purpose: time comes from the portable utils and _thread is imported
only inside start(), so the module loads on CPython (host tooling, CI) and on MicroPython
builds compiled without _thread alike.
"""

from AlLoRa.File import AlLoRa_File
from AlLoRa.utils.time_utils import sleep
from AlLoRa.utils.os_utils import os
from AlLoRa.utils.debug_utils import print


# Do not instanciate this class as pretends to be an abstract one
class DataSource:

    def __init__(self, file_chunk_size: int, file_queue_size=25, sleep_between_readings=60):

        self.STOP_THREAD = True
        self.IS_STARTED = False

        self.file_queue = []
        self.file_queue_size = file_queue_size
        self.file_chunk_size = file_chunk_size

        self.SECONDS_BETWEEN_READINGS = sleep_between_readings

        self.backup_file = None

    def get_file_chunk_size(self):
        return self.file_chunk_size

    def add_to_queue(self, file: AlLoRa_File):
        if len(self.file_queue) >= self.file_queue_size:
            self.file_queue.pop(0)

        repeated = False
        for f in self.file_queue:
            if f.get_name() == file.get_name():
                repeated = True
                break
        if repeated is False:
            self.file_queue.append(file)

    def read(self):
        while self.STOP_THREAD is False:
            try:
                file = self.read_datasource()
                if file is not None:
                    self.add_to_queue(file=file)
                else:
                    print("skipped file, it is None")
                sleep(self.SECONDS_BETWEEN_READINGS)
            except KeyboardInterrupt as e:
                self.stop()
        self.IS_STARTED = False
        print(self.STOP_THREAD)

    def read_datasource(self) -> AlLoRa_File:
        pass

    # --- cooperative surface: feed the queue from the node's own loop, no thread ---------

    def check(self):
        """Non-blocking pump, called by the node loop each round (it shares the radio
        loop, so it must never block). The base has nothing to poll; a subclass that
        ingests from a live feed (a broker, a bus) drains it into the queue here."""
        pass

    def has_pending(self):
        return bool(self.file_queue)

    def peek_file(self):
        """The head of the queue WITHOUT consuming it, or None. A server that may fail
        mid-delivery peeks, serves, and only confirms once delivery completed, so a
        failed attempt retries the same file (at-least-once, no delivered-marker)."""
        return self.file_queue[0] if self.file_queue else None

    def confirm_file(self):
        """Delivery completed: drop the head off the queue and return it (None if empty).
        The pop half of peek_file's peek-retain contract."""
        return self.file_queue.pop(0) if self.file_queue else None

    def prepare(self):
        pass

    def close(self):
        """Release whatever prepare() acquired (a client, a handle). Idempotent; the
        base acquires nothing."""
        pass

    def start(self):
        self.prepare()
        self.STOP_THREAD = False
        self.IS_STARTED = True
        print("DataSource Starting!")
        import _thread
        _thread.start_new_thread(self.read, ())

    def stop(self):
        self.STOP_THREAD = True
        print(self.STOP_THREAD)

    def is_started(self):
        return self.IS_STARTED

    '''
    Everytime this function is called, it assumes the file is already consumed, so a deleting is performed.
    '''
    def get_next_file(self):
        try:
            backup_file = self.file_queue.pop(0) # ER: I don't think is should be managed with an exception, not a good practice
            #self.backup(file=backup_file) # ER: is raising exception with compressed files
            return backup_file
        except Exception as e:
            return None

    def get_backup(self):
        try:
            filename = ""
            with open("./filename-backup.txt", "r") as f:
                filename = f.read()

            content = None
            with open("./content-backup", "r") as f:
                content = f.read()
            rescued_file = AlLoRa_File(name='{}'.format(filename), content=bytearray(content), chunk_size=self.file_chunk_size)
            return rescued_file
        except OSError as e:
            print("The backup could not be restored", e)

    def backup(self, file: AlLoRa_File):
        try:
            os.remove("./filename-backup.txt")
            os.remove("./content-backup")
        except OSError:
            pass

        with open("./filename-backup.txt", "w") as f:
            f.write(file.get_name())

        with open("./content-backup", "w") as f:
            f.write(file.get_content())

import _thread
import utime
import os

from AlLoRa.File import AlLoRa_File

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
                utime.sleep(self.SECONDS_BETWEEN_READINGS)
            except KeyboardInterrupt as e:
                self.stop()
        self.IS_STARTED = False
        print(self.STOP_THREAD)


    def read_datasource(self) -> AlLoRa_File:
        pass


    def prepare(self):
        pass

    def start(self):
        self.prepare()
        self.STOP_THREAD = False
        self.IS_STARTED = True
        print("DataSource Starting!")
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

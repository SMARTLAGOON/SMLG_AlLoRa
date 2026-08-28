# The microSD card on a board that has one.
#
# Pins are not restated here. They are read off the board object, which is the one file that
# knows this board's wiring: the previous version carried its own defaults, one of which
# (MOSI 15) contradicted the board file (11), so a caller who trusted the signature got a
# card that never mounted and a node that ran on and served nothing.
import gc
import machine

from AlLoRa.utils.os_utils import os


class SD_manager:

    def __init__(self, board, path=None):
        gc.enable()
        self.root = path if path is not None else board.SD_MOUNT_POINT
        # No try/except. A caller who needs the card needs to hear about it: swallowing the
        # failure here is what let a board with an unreadable card look healthy while its
        # outbound queue pointed at a directory that was not there.
        self.sd = machine.SDCard(slot=2,
                                 sck=machine.Pin(board.SD_SCLK),
                                 mosi=machine.Pin(board.SD_MOSI),
                                 miso=machine.Pin(board.SD_MISO),
                                 cs=machine.Pin(board.SD_CS))
        os.mount(self.sd, self.root)

    def get_path(self):
        return self.root

    def get_files(self):
        return os.listdir(self.root)

    def get_format_files(self, format):
        return [file for file in self.get_files() if file.endswith(format)]

    def erase_file(self, file):
        os.remove(self.root + '/' + file)
        gc.collect()

    def move_file(self, file, destination):
        os.rename(file, destination)
        gc.collect()

    def create_file(self, file_name, content):
        f = open(self.root + '/' + file_name, 'w')
        f.write(content)
        f.close()
        gc.collect()

    def make_dir(self, name):
        """Ensure a directory exists on the card, and answer with its full path."""
        path = self.root + '/' + name.strip('/')
        try:
            os.listdir(path)
        except OSError:
            os.mkdir(path)
        return path

    def unmount(self):
        os.umount(self.root)
        gc.collect()

    def mount(self):
        os.mount(self.sd, self.root)

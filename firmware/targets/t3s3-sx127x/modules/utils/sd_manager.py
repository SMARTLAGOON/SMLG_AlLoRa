import os
import machine
import gc

from utils.OnDemandFile import OnDemandFile

class SD_manager:

    def __init__(self, path='/sd', sclk = 14, mosi = 15, miso = 2, cs = 13):
        gc.enable()
        self.root = path
        try:
            self.sd = machine.SDCard(slot=2, sck= machine.Pin(sclk), 
                                        mosi= machine.Pin(mosi), miso= machine.Pin(miso), cs= machine.Pin(cs))

            os.mount(self.sd, self.root)
        
        except Exception as e:
            print(e)
            print("SD card not found")
            self.sd = None

    def get_path(self):
        return self.root

    def get_files(self):
        return os.listdir(self.root)
    
    def get_format_files(self, format):
        return [file for file in self.get_files() if file.endswith(format)]
    
    def get_file(self, file_name):
        return OnDemandFile(self.root + '/' + file_name)
    
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

    def unmount(self):
        os.umount(self.root)
        gc.collect()
    
    def mount(self):
        os.mount(self.sd, self.root)

    def __exit__(self):
        self.unmount()
        del self.sd
        del self.l
        gc.collect()

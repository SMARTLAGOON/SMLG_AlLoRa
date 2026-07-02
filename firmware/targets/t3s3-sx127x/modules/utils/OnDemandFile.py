
class OnDemandFile:
    def __init__(self, filename):
        self.filename = filename
        self.file = open(filename, 'rb')
        self.pos = 0

    def __getitem__(self, index):
        if isinstance(index, int):
            if index != self.pos:
                self.file.seek(index)
                self.pos = index
            self.pos += 1
            return self.file.read(1)[0]
        elif isinstance(index, slice):
            start, stop, step = index.indices(self.__len__())
            if start != self.pos:
                self.file.seek(start)
                self.pos = start
            self.pos = stop
            return self.file.read(stop - start)

    def __len__(self):
        current_pos = self.file.tell()
        self.file.seek(0, 2)
        length = self.file.tell()
        self.file.seek(current_pos)
        return length
    
    def close(self):
        self.file.close()

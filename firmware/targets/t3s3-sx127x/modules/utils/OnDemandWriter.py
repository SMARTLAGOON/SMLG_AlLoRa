
class OnDemandFileWriter:
    def __init__(self, filename):
        self.file = open(filename, 'wb')

    def write(self, data):
        self.file.write(data)

    def close(self):
        self.file.close()

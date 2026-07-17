from AlLoRa.DataSinks.DataSink import DataSink


class Disk_DataSink(DataSink):
    """The default sink: persist each received file under result_path/<source>/<name>, exactly
    as the Collector did before the sink seam existed. AlLoRa_File.save() renames the reassembly
    temp into place, so this holds no whole file in RAM (it matches the streamed-to-disk design).
    """

    def __init__(self, result_path="Results"):
        self.result_path = result_path

    def consume(self, file, reception=None):
        # Byte-identical to the legacy `file.save(result_path + "/" + mac)`: a per-source subfolder
        # when we know the source, else straight into result_path.
        source = reception.source if reception is not None else None
        path = self.result_path if source is None else self.result_path + "/" + source
        file.save(path)

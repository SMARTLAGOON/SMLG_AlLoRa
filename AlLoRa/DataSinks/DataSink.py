class Reception:
    """The completion record handed to a DataSink alongside a finished file: an immutable
    snapshot of *who sent this and how it arrived*, taken at the instant of delivery.

    It is deliberately separate from the live status stream (`Node.notify_subscribers`, which
    ticks RSSI/chunk-progress *during* a transfer): that stream is for a progress UI, this is the
    authoritative "file X from source Y finished, here are its final stats" event. A sink gets it
    without having to also subscribe to the status channel, and it is a frozen copy — never the
    live `Digital_Endpoint`, which gets reused/mutated for the next file.
    """

    def __init__(self, source, session_id=None, device_id=None,
                 rssi=None, snr=None, total_chunks=None, timestamp_ms=None):
        self.source = source              # the Source's MAC (the folder / topic key)
        self.session_id = session_id      # sid
        self.device_id = device_id        # did (v3 secure); None in open mode
        self.rssi = rssi                  # final RF snapshot
        self.snr = snr
        self.total_chunks = total_chunks  # transfer size/quality
        self.timestamp_ms = timestamp_ms  # when it landed


# Do not instanciate this class as pretends to be an abstract one
class DataSink:
    """The Collector's completed-file output boundary — the symmetric twin of DataSource.

    DataSource feeds AlLoRa_File objects *in* to a Source; DataSink drains the files a Collector
    reassembles *out* to wherever a deployment wants them (disk, MQTT, cloud, a management
    website). Before this seam existed the Collector hardwired a disk save, so "what to do with
    the data" leaked into the node; a DataSink lets that decision live at the application layer,
    swappable, with the node knowing only "hand the finished file to my sink."

    Subclass and override consume(). Optionally override prepare()/close() for a sink that owns a
    connection (a broker client, an open socket); the default lifecycle is a no-op so a trivial
    sink needs neither.
    """

    def prepare(self):
        """Bring the sink up (connect a client, open a handle). Called at most once before the
        first consume() that needs it; a self-contained sink may leave it a no-op."""
        pass

    def consume(self, file, reception=None):
        """Take ownership of one completed AlLoRa_File. `reception` is a Reception snapshot of the
        transfer (source identity + final stats). Reading the bytes is only the common first step:
        a sink may store, transform, forward, publish, or delete as it sees fit. Silently dropping
        a received file is worse than any error, so the base refuses to no-op."""
        raise NotImplementedError("DataSink subclasses must implement consume(file, reception)")

    def close(self):
        """Release any resource prepare() acquired. Idempotent; safe to call without prepare()."""
        pass

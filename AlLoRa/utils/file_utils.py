"""Writing the small files a node cannot afford to lose."""
from AlLoRa.utils.os_utils import os


def commit_file(path, text):
    """Replace a file's contents with `text`, all of it or none of it.

    Write to a temporary file and rename it into place, the pattern File.py already runs on
    device for chunk reassembly. Every file that reaches here is one a node reads on the next
    boot to know what it is: its own radio config, its roster, the number its control plane
    counts from, its identity. Interrupted part way through a direct write, each of them comes
    back as something worse than out of date. Truncated JSON reads as an empty mark, which
    restarts a sequence the fleet has already moved past; a half-written identity scalar still
    parses, so the node comes back working and quietly carrying half the key it was given.

    None of that is recoverable over the radio, and all of it is avoided by never writing to
    the name the node boots from.
    """
    temp = path + ".tmp"
    with open(temp, "w") as f:
        f.write(text)
    try:
        os.rename(temp, path)
    except OSError:
        # A FAT volume refuses to rename onto a name already in use, where littlefs replaces
        # the target instead, and an ESP32 can be flashed either way. Clearing the way first
        # narrows the window to two filesystem operations rather than dropping the write
        # altogether, and the complete new file sits in `temp` throughout it.
        os.remove(path)
        os.rename(temp, path)

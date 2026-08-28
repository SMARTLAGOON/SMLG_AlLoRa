"""The small files a node cannot afford to lose: writing them, and finding the one it boots from."""
from AlLoRa.utils.os_utils import os

# The config filename, current first and historical second. Both are read forever.
CONFIG_NAMES = ("AlLoRa.json", "LoRa.json")


def resolve_config_file(config_file=None):
    """The config file a node boots from: the caller's choice, or the first name that exists.

    `LoRa.json` names the one section of the file that is actually about the radio. The file
    also carries the posture, the identity path, the control root, the result path, the
    protocol version and what the board is, so the name moved to `AlLoRa.json`.

    The old name is read forever rather than for a migration window. There are boards deployed
    in places this project cannot reach, student repositories vendoring this library, and a
    published paper trail. A name that stops being read is a node that stops booting for a
    reason nobody standing at the antenna can see, and the whole cost of keeping it is this
    loop.
    """
    if config_file is not None:
        return config_file
    for name in CONFIG_NAMES:
        try:
            # `open` rather than a stat: MicroPython has no os.path, and a name that cannot be
            # opened is not a config file this node can boot from whatever the directory says.
            open(name, "r").close()
            return name
        except OSError:
            continue
    raise OSError(
        "no config file: looked for {} in the working directory. A node reads what it is "
        "from that file, so there is no default to come up on.".format(
            " and then ".join(CONFIG_NAMES)))


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
    _commit(path, text, "w")


def commit_bytes(path, data):
    """`commit_file` for a payload rather than a config: same all-or-nothing rename.

    What a queued outbound file needs is the same guarantee for a different reason. The
    node does not read this one back to learn what it is; it reads it back to send it. A
    half-written payload is not a node that boots wrong, it is a reading that crosses the
    link truncated and is believed, because nothing downstream can tell a short file from
    a small one.
    """
    _commit(path, data, "wb")


def _commit(path, data, mode):
    temp = path + ".tmp"
    with open(temp, mode) as f:
        f.write(data)
    try:
        os.rename(temp, path)
    except OSError:
        # A FAT volume refuses to rename onto a name already in use, where littlefs replaces
        # the target instead, and an ESP32 can be flashed either way. Clearing the way first
        # narrows the window to two filesystem operations rather than dropping the write
        # altogether, and the complete new file sits in `temp` throughout it.
        os.remove(path)
        os.rename(temp, path)

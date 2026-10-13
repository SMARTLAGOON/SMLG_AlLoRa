"""Runs the AlLoRa library as tagged v2.0.0, in its own process, with a pipe for a radio.

The v2.0.0 package is also named `AlLoRa`, so it cannot share a process with the library under
test. `test_v2_tag_interop.py` extracts the tag into a folder and starts this script with that
folder first on the path, then relays frames between this process and the other side.

Frames cross the pipe as a two-byte big-endian length followed by the frame's bytes. One
empty frame comes first and means "listening": without it, a Hub that starts sending while
this interpreter is still importing would time out and resend. stdout carries frames only;
whatever the old library prints goes to stderr.

    python v2_tag_peer.py <v2 tree> edge <config> <own mac> <file to serve>
    python v2_tag_peer.py <v2 tree> hub  <config> <own mac> <edge mac>
"""
import os
import select
import struct
import sys

sys.path.insert(0, sys.argv[1])

from AlLoRa.Connectors.Connector import Connector  # noqa: E402  (the v2.0.0 one)

_out = sys.stdout.buffer
sys.stdout = sys.stderr


def _read_exact(n):
    # Raw reads on the descriptor: a buffered reader can hold a frame that select() cannot see.
    buf = b""
    while len(buf) < n:
        part = os.read(0, n - len(buf))
        if not part:
            break
        buf += part
    return buf


class PipeConnector(Connector):

    def __init__(self, mac):
        super().__init__()
        self.MAC = mac

    def send(self, packet):
        wire = packet.get_content()
        _out.write(struct.pack("!H", len(wire)) + wire)
        _out.flush()
        return True

    def recv(self, focus_time=12):
        ready, _, _ = select.select([0], [], [], focus_time)
        if not ready:
            return None
        size = _read_exact(2)
        if len(size) < 2:
            sys.exit(0)    # the other side has gone
        return _read_exact(struct.unpack("!H", size)[0])


def _ready():
    _out.write(struct.pack("!H", 0))
    _out.flush()


def main():
    role, config, own_mac, arg = sys.argv[2:6]
    if role == "edge":
        from AlLoRa.Nodes.Source import Source
        from AlLoRa.File import CTP_File
        with open(arg, "rb") as f:
            content = f.read()
        source = Source(PipeConnector(own_mac), config_file=config)
        source.set_file(CTP_File(name=os.path.basename(arg), content=bytearray(content),
                                 chunk_size=source.get_chunk_size()))
        _ready()
        # v2.0.0 counts this timeout in milliseconds.
        sys.exit(0 if source.send_file(timeout=30000) else 1)
    if role == "hub":
        from AlLoRa.Nodes.Requester import Requester
        from AlLoRa.Digital_Endpoint import Digital_Endpoint
        requester = Requester(PipeConnector(own_mac), config_file=config)
        endpoint = Digital_Endpoint(name="edge", mac_address=arg, active=True)
        _ready()
        requester.listen_to_endpoint(endpoint, listening_time=30, save_file=True, one_file=True)
        sys.exit(0)
    sys.exit("unknown role: " + role)


if __name__ == "__main__":
    main()

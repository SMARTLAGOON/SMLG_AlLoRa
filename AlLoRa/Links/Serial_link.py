"""Serial_link: a UART byte-mover for the split Connector.

The concrete `Link` under a serial tunnel: it moves opaque request/reply frames between the
logic-holder (a Raspberry-Pi/host over `pyserial`) and the bridge (an ESP32 over
`machine.UART`), and knows nothing about the verbs, the LoRa air wire, or session keys. A
frame is delimited on the wire by a sentinel; the frame bytes themselves are the JSON that
`tunnel_codec` produces, which is pure printable ASCII (the LoRa blob rides as hex inside it),
so the sentinel `<<END>>\\n` can never occur mid-frame and a text-only UART is safe.

`text_safe` keeps the printable-only filter the old Serial adapter relied on: UART line noise
on an ESP32 (boot chatter, brown-outs) injects stray bytes, and dropping the non-printable
ones lets a valid frame survive noise instead of failing to parse and forcing a retransmit.

Both halves share one implementation over a small `port` (write / read / bytes-available); the
client and bridge differ only in which physical port they hold. `Loopback_link` covers the
in-process path; this covers the real UART, whose final proof is on hardware.
"""
from AlLoRa.Links.Link import Link
from AlLoRa.utils.time_utils import current_time_ms as _now_ms, sleep_ms


class Serial_link(Link):

    SENTINEL = b"<<END>>\n"

    def __init__(self, port, sentinel=SENTINEL, poll_ms=5, text_safe=True):
        self._port = port
        self._sentinel = sentinel
        self._poll_ms = poll_ms
        self._text_safe = text_safe
        self._buf = bytearray()
        # One availability probe, chosen once: pyserial exposes `in_waiting`, machine.UART
        # exposes `any()`. Without either we fall back to a blocking 1-byte read on the port.
        if hasattr(port, "in_waiting"):
            self._available = lambda: port.in_waiting
        elif hasattr(port, "any"):
            self._available = port.any
        else:
            self._available = None

    # --- construction: the two physical ports a serial tunnel binds to -----------------------

    @classmethod
    def client(cls, serial_port, baud=9600, timeout=1, **kwargs):
        """Logic-holder half over `pyserial` (imported lazily so the module loads without it)."""
        import serial
        port = serial.Serial(serial_port, baud, timeout=timeout)
        return cls(port, **kwargs)

    @classmethod
    def bridge(cls, uartid=0, baud=9600, tx=None, rx=None, bits=8, parity=None, stop=1,
               timeout=800, **kwargs):
        """Bridge (Adapter) half over `machine.UART` (imported lazily; device-only)."""
        from machine import UART
        uart = UART(uartid, baud)
        uart.init(baudrate=baud, tx=tx, rx=rx, bits=bits, parity=parity, stop=stop,
                  timeout=timeout)
        return cls(uart, **kwargs)

    # --- Link contract -----------------------------------------------------------------------

    def rpc(self, request, timeout=None):
        # A fresh request supersedes anything still buffered from a request we already gave up
        # on (the protocol layer retransmits, so a stale reply must not answer the new one).
        self._flush_input()
        self._port.write(request + self._sentinel)
        return self._read_frame(timeout)

    def read_request(self, timeout=None):
        return self._read_frame(timeout)

    def write_reply(self, reply):
        self._port.write(reply + self._sentinel)

    # --- framing -----------------------------------------------------------------------------

    def _read_frame(self, timeout):
        """Accumulate bytes until the sentinel, then return the frame before it (the sentinel
        and anything after it stay buffered for the next read). None once `timeout` elapses."""
        deadline = None if timeout is None else _now_ms() + int(timeout * 1000)
        s = self._sentinel
        buf = self._buf
        while True:
            idx = buf.find(s)
            if idx >= 0:
                frame = bytes(buf[:idx])
                del buf[:idx + len(s)]
                return frame
            if deadline is not None and _now_ms() >= deadline:
                return None
            chunk = self._drain()
            if chunk:
                if self._text_safe:
                    chunk = bytes(b for b in chunk if 32 <= b <= 126 or b in (10, 13))
                buf.extend(chunk)
                continue
            sleep_ms(self._poll_ms)

    def _drain(self):
        # Whatever is available right now, uniformly across pyserial / machine.UART.
        if self._available is None:
            b = self._port.read(1)     # no probe: lean on the port's own read timeout
            return b or b""
        n = self._available()
        if not n:
            return b""
        b = self._port.read(n)
        return b or b""

    def _flush_input(self):
        self._buf = bytearray()
        while self._drain():
            pass

    def close(self):
        try:
            self._port.close()
        except Exception:
            pass

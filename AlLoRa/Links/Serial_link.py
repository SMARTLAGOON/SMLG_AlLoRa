"""Serial_link: a byte-mover for the split Connector, over a UART or over USB-CDC.

The concrete `Link` under a serial tunnel: it moves opaque request/reply frames between the
logic-holder (a Raspberry-Pi/host over `pyserial`) and the bridge (an ESP32 over
`machine.UART`, or over its own USB console), and knows nothing about the verbs, the LoRa air
wire, or session keys. A frame is delimited on the wire by a sentinel; the frame bytes
themselves are the JSON that `tunnel_codec` produces, which is pure printable ASCII (the LoRa
blob rides as hex inside it), so the sentinel `<<END>>\\n` can never occur mid-frame and a
text-only UART is safe.

`text_safe` keeps the printable-only filter the old Serial adapter relied on: UART line noise
on an ESP32 (boot chatter, brown-outs) injects stray bytes, and dropping the non-printable
ones lets a valid frame survive noise instead of failing to parse and forcing a retransmit.
`_resync` is its other half, for the noise that *is* printable: see below.

Both halves share one implementation over a small `port` (write / read / bytes-available); the
client and bridge differ only in which physical port they hold. There are three such ports and
they cover every way a board can be reached:

  * `client()`   the host end, `pyserial` on a device path. A USB-CDC board is an ordinary
                 serial device to the host, so this end is the same for both wirings.
  * `bridge()`   a board with a `machine.UART`: GPIO pins, or a board whose USB socket goes
                 through a USB-to-serial chip (LoPy4, a Grove-to-USB E5).
  * `bridge_usb()` a board whose USB socket is native USB (the T3S3, and other ESP32-S3,
                 ESP32-C3 and RP2040 boards), where there is no UART behind the port and the
                 console itself is the only handle.

`Loopback_link` covers the in-process path; this covers the real wire, whose final proof is on
hardware.
"""
from AlLoRa.Links.Link import Link
from AlLoRa.utils.time_utils import current_time_ms as _now_ms, sleep_ms


class _Stdio_port:
    """The board's USB-CDC console, wrapped in the small port contract Serial_link expects.

    On a native-USB board MicroPython's console is the USB CDC endpoint and there is no
    `machine.UART` behind it, so `sys.stdin` / `sys.stdout` are the only handle on those bytes.
    Wrapping them here means the USB tunnel reuses the framing the GPIO tunnel already proved
    rather than restating it: USB-CDC is still sentinel-framed serial, only the port differs.

    The REPL is not a competitor for these bytes. MicroPython runs `main.py` to completion
    before it starts the REPL, so for as long as the Adapter's loop is running, stdin belongs
    to the Adapter. Ctrl-C is left armed deliberately: a tunnel frame is printable ASCII plus
    the sentinel and can never contain 0x03, so the only thing that can raise KeyboardInterrupt
    is a human, and `Adapter.run()` catches it and falls back to the REPL. On a board whose data
    port is also its console, that is the way back in.
    """

    # The console reports no byte count, so `any()` promises a batch and `read()` stops early
    # when it dries up. Reading one byte per poll would work but costs a Python-level loop
    # iteration per byte of every frame, and on-device CPU is a design invariant.
    READ_BATCH = 64

    def __init__(self, stdin=None, stdout=None):
        import sys
        _in = stdin if stdin is not None else sys.stdin
        _out = stdout if stdout is not None else sys.stdout
        # The console is a text stream; `.buffer` is its byte half. CPython always exposes it
        # and MicroPython does on the ports that matter, but fall back rather than fail: a port
        # that hands out bytes directly is just as good here.
        self._in = getattr(_in, "buffer", _in)
        self._out = getattr(_out, "buffer", _out)
        import select
        self._poll = select.poll()
        self._poll.register(self._in, select.POLLIN)

    def any(self):
        return self.READ_BATCH if self._poll.poll(0) else 0

    def read(self, n):
        # Never block: take only what poll has already promised. A blocking console read would
        # stall the Adapter's serve loop past its own timeout, which would make the bridge deaf
        # to both the next request and to Ctrl-C.
        out = bytearray()
        while len(out) < n and self._poll.poll(0):
            b = self._in.read(1)
            if not b:
                break
            out.extend(b)
        return bytes(out)

    def write(self, data):
        self._out.write(data)
        # CPython buffers stdout, and a buffered reply is a reply the host never sees until the
        # next one pushes it out. Not every MicroPython console object has flush, hence the probe.
        flush = getattr(self._out, "flush", None)
        if flush is not None:
            flush()
        return len(data)

    def close(self):
        pass


class Serial_link(Link):

    SENTINEL = b"<<END>>\n"

    # A tunnel_codec frame is a flat JSON object, so it opens with `{` and contains no other.
    # That is what lets a reader find where a frame *starts*; the sentinel only says where one
    # ends. `tests/test_usb_adapter.py` holds the invariant so it cannot rot silently.
    FRAME_START = b"{"

    def __init__(self, port, sentinel=SENTINEL, poll_ms=5, text_safe=True):
        self._port = port
        self._sentinel = sentinel
        self._poll_ms = poll_ms
        self._text_safe = text_safe
        self._buf = bytearray()
        # Bytes discarded ahead of a frame start. Nonzero is not an error (a board that resets
        # mid-session is normal), but a number that keeps climbing during a transfer means
        # something on the far side is writing to the wire while the tunnel is using it.
        self.preamble_dropped = 0
        # One availability probe, chosen once: pyserial exposes `in_waiting`, machine.UART
        # exposes `any()`. Without either we fall back to a blocking 1-byte read on the port.
        if hasattr(port, "in_waiting"):
            self._available = lambda: port.in_waiting
        elif hasattr(port, "any"):
            self._available = port.any
        else:
            self._available = None

    # --- construction: the three physical ports a serial tunnel binds to ---------------------

    @classmethod
    def client(cls, serial_port, baud=9600, timeout=1, **kwargs):
        """Logic-holder half over `pyserial` (imported lazily so the module loads without it).

        This is also the host end of a *USB* tunnel, unchanged: a CDC board is an ordinary
        serial device to the host, so only the path differs (`/dev/ttyACM0` on a Pi,
        `/dev/cu.usbmodem*` on macOS). `baud` is honoured by a real UART and ignored by CDC,
        which runs at USB speed whatever it is told.
        """
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

    @classmethod
    def bridge_usb(cls, stdin=None, stdout=None, **kwargs):
        """Bridge (Adapter) half over the board's own USB-CDC console (device-only in practice;
        `stdin`/`stdout` are injectable so the path is testable off-device).

        The input direction is drained once here. A host that opened the port and started
        talking while the board was still coming up has left bytes in front of the first real
        request, and unlike the reply direction there is no `_resync` anchor to recover them:
        they are simply stale.
        """
        link = cls(_Stdio_port(stdin=stdin, stdout=stdout), **kwargs)
        link._flush_input()
        return link

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
                return self._resync(frame)
            if deadline is not None and _now_ms() >= deadline:
                return None
            chunk = self._drain()
            if chunk:
                if self._text_safe:
                    chunk = bytes(b for b in chunk if 32 <= b <= 126 or b in (10, 13))
                buf.extend(chunk)
                continue
            sleep_ms(self._poll_ms)

    def _resync(self, frame):
        """Drop whatever the medium injected ahead of the frame.

        The sentinel says where a frame ends, never where it starts, so anything the far side
        wrote before it is still sitting in front of the JSON: an ESP-IDF boot line, a
        MicroPython banner, a debug line from code that did not know the console was a wire.
        `text_safe` cannot help, because all of that is perfectly printable. Without this the
        preamble reaches the JSON parser and a good frame is thrown away as corrupt, costing a
        retransmit and, at the head of a session, the whole handshake.

        On a dedicated UART the case was rare enough to live with. On a USB bridge, whose
        console *is* the tunnel, it happens on every single reset, which is why the fix belongs
        here rather than in the USB path: the pin rig has the same latent bug, just less often.

        Taking the *last* frame start rather than the first is what makes a preamble containing
        a brace of its own recoverable; a real frame has exactly one.

        The scan uses `find` rather than `rfind` on purpose. `find` is the call the sentinel
        search above already makes, so the device has been running it since the first tunnel;
        `rfind` would be the only one of its kind in the library, and a method that turns out to
        be absent from a MicroPython build fails here on every frame, which is to say the tunnel
        does not work at all and only a board would tell us. Not worth the four lines saved.
        """
        if frame[:1] == self.FRAME_START:
            return frame          # already clean, which is what nearly every frame is
        i = -1
        at = 0
        while True:
            j = frame.find(self.FRAME_START, at)
            if j < 0:
                break
            i = j
            at = j + 1
        if i <= 0:
            # -1 is a frame with nothing frame-shaped in it, which is the caller's problem to
            # report rather than ours to silently empty.
            return frame
        self.preamble_dropped += i
        return frame[i:]

    def _drain(self):
        # Whatever is available right now, uniformly across pyserial / machine.UART / console.
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

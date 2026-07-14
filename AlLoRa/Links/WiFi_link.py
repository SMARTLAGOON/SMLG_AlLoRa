"""WiFi_link — an HTTP byte-mover for the split Connector.

The concrete `Link` under a WiFi tunnel: the logic-holder half POSTs an opaque request frame
to the bridge (Adapter) and reads the reply frame back as the HTTP body; the bridge half
accepts one connection, hands the posted body up as the request, and writes the reply body
back on the same socket. Like every Link it carries opaque bytes only — the LoRa frame rides
as hex inside the `tunnel_rpc` JSON body, so the bridge never parses it and holds no key.

The request/reply lockstep maps onto one HTTP round trip per verb: `read_request` accepts and
reads the body, `write_reply` answers on the held socket and closes it. Client and bridge use
the same BSD socket surface, so `socket` (host/CPython) and `usocket` (ESP32/MicroPython)
both drive it; the module to use is injected, defaulting to whichever is importable. Network
bring-up (AP/STA) is the WiFi_Interface's job — this link only moves bytes over an
already-up network, whose final proof is on hardware.
"""
from AlLoRa.Links.Link import Link


def _default_socket_module():
    try:
        import usocket
        return usocket
    except ImportError:
        import socket
        return socket


class WiFi_link(Link):

    def __init__(self, socket_module=None):
        self._socket = socket_module or _default_socket_module()

    # --- construction --------------------------------------------------------------------------

    @classmethod
    def client(cls, host="192.168.4.1", port=80, timeout=20, recv_size=4096,
               path="/command", socket_module=None):
        """Logic-holder half: opens a fresh connection to the bridge per `rpc`."""
        link = cls(socket_module)
        link._host = host
        link._port = port
        link._timeout = timeout
        link._recv_size = recv_size
        link._path = path
        return link

    @classmethod
    def bridge(cls, host="0.0.0.0", port=80, backlog=1, socket_module=None):
        """Bridge (Adapter) half: binds + listens now (network must already be up)."""
        link = cls(socket_module)
        link._recv_size = 4096
        s = link._socket.socket(link._socket.AF_INET, link._socket.SOCK_STREAM)
        s.setsockopt(link._socket.SOL_SOCKET, link._socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(backlog)
        link._server = s
        link._client = None
        return link

    def bound_port(self):
        # The actually-bound port (useful when binding to port 0 for tests).
        return self._server.getsockname()[1]

    # --- client half ---------------------------------------------------------------------------

    def rpc(self, request, timeout=None):
        s = self._socket.socket(self._socket.AF_INET, self._socket.SOCK_STREAM)
        s.settimeout(timeout if timeout is not None else self._timeout)
        try:
            addr = self._socket.getaddrinfo(self._host, self._port)[0][-1]
            s.connect(addr)
            body = bytes(request)
            head = ("POST {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n"
                    "Content-Type: application/json\r\nContent-Length: {}\r\n\r\n").format(
                        self._path, self._host, len(body)).encode()
            s.send(head + body)
            raw = self._recv_until_close(s)
            return self._http_body(raw)
        except Exception:
            return None                       # link/timeout failure reads as a radio timeout
        finally:
            try:
                s.close()
            except Exception:
                pass

    # --- bridge half ---------------------------------------------------------------------------

    def read_request(self, timeout=None):
        self._server.settimeout(timeout)
        try:
            client, _ = self._server.accept()
        except Exception:
            return None                       # accept timed out: no request this window
        self._client = client
        try:
            raw = self._recv_http_request(client)
            return self._http_body(raw)
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            self._client = None
            return None

    def write_reply(self, reply):
        if self._client is None:
            return
        body = bytes(reply)
        head = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                "Content-Length: {}\r\nConnection: close\r\n\r\n").format(len(body)).encode()
        try:
            self._client.send(head + body)
        finally:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def close(self):
        for sock in (getattr(self, "_client", None), getattr(self, "_server", None)):
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    # --- HTTP helpers --------------------------------------------------------------------------

    def _recv_until_close(self, sock):
        chunks = []
        while True:
            chunk = sock.recv(self._recv_size)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    def _recv_http_request(self, sock):
        # Read headers, then exactly Content-Length body bytes. Our client always sends a
        # Content-Length, and the body (tunnel_rpc JSON) never contains a CRLF-CRLF, so the
        # header split is unambiguous.
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(self._recv_size)
            if not chunk:
                break
            data += chunk
        header, _, body = data.partition(b"\r\n\r\n")
        length = self._content_length(header)
        while length is not None and len(body) < length:
            chunk = sock.recv(self._recv_size)
            if not chunk:
                break
            body += chunk
        return header + b"\r\n\r\n" + body

    @staticmethod
    def _content_length(header):
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    return int(line.split(b":", 1)[1].strip())
                except Exception:
                    return None
        return None

    @staticmethod
    def _http_body(raw):
        if not raw:
            return None
        _, sep, body = raw.partition(b"\r\n\r\n")
        return body if sep else None

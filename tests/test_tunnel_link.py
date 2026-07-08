"""Unit — the Link seam (byte pipe between a split Connector's two halves).

A Link moves opaque request/reply frames and knows nothing about verbs, the LoRa wire, or
keys — so a new transport (serial, WiFi, BLE, USB) is a new Link, not new protocol logic.
The Loopback_link backs it with a pair of in-process queues, so the whole tunnel is testable
on CPython without a UART or a socket (the same trick the Loopback_connector plays for radios).
"""
import threading

from AlLoRa.Links.Loopback_link import Loopback_link


def test_rpc_delivers_the_request_and_returns_the_bridge_reply():
    client, bridge = Loopback_link.create_pair()

    def serve():
        req = bridge.read_request(timeout=2)
        assert req == b"ping"
        bridge.write_reply(b"pong")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert client.rpc(b"ping") == b"pong"
    t.join(timeout=2)
    assert not t.is_alive()


def test_read_request_times_out_to_none_when_idle():
    _, bridge = Loopback_link.create_pair()
    assert bridge.read_request(timeout=0.2) is None


def test_requests_and_replies_do_not_cross_direction():
    # The down queue (requests) and up queue (replies) are independent: a pending reply must
    # never be read back as the next request, or the pump would echo its own output.
    client, bridge = Loopback_link.create_pair()

    def serve():
        for _ in range(2):
            req = bridge.read_request(timeout=2)
            bridge.write_reply(req + b"!")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert client.rpc(b"a") == b"a!"
    assert client.rpc(b"b") == b"b!"
    t.join(timeout=2)

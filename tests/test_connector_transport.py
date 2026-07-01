"""Unit — the narrowed Connector transport verbs (transmit / listen / exchange).

Step 3 of the exchange decomposition narrows the Connector to pure byte transport:
`transmit(wire)` puts framed bytes on the wire, `listen(window) -> (wire, td)` runs one
timed receive window at the radio, and `exchange(wire, window, match_key) -> (reply, td,
status)` composes them into transmit + wait-for-the-matching-reply — codec-free and keyless
(it matches on the wire prefix), so a dumb tunnel bridge can run it. `send(packet)` stays as
a back-compat shim that frames via the codec then transmits.

The Loopback connector backs these with in-process queues, so they're testable on CPython.
"""
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Codec import V3OpenCodec
from AlLoRa.Packet_v3 import Packet_v3


def _pair():
    return Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")


def _req(sid):
    p = Packet_v3(addressing="sid"); p.set_session(sid); p.ask_metadata()
    return p


def _reply(sid):
    p = Packet_v3(addressing="sid"); p.set_session(sid); p.set_ok()
    return p


def test_transmit_then_listen_delivers_the_bytes():
    a, b = _pair()
    assert a.transmit(b"hello") is True
    wire, td = b.listen(1.0)
    assert wire == b"hello"
    assert td >= 0


def test_listen_times_out_to_none():
    a, _ = _pair()
    wire, td = a.listen(0.3)
    assert wire is None
    assert td >= 0.2          # roughly the window elapsed with nothing to receive


def test_send_shim_frames_via_the_codec_then_transmits():
    a, b = _pair()
    a.codec = V3OpenCodec(addressing="sid")     # the shim uses whatever codec is configured
    p = Packet_v3(addressing="sid"); p.set_session(5); p.set_data(b"hi")
    assert a.send(p) is True
    wire, _ = b.listen(1.0)
    assert wire == p.get_content()              # send(packet) == transmit(codec.frame(packet))


def test_exchange_returns_the_matching_reply():
    a, b = _pair()
    a.min_timeout = 0.5
    match_key = V3OpenCodec(addressing="sid").match_spec(_req(7))
    reply_wire = _reply(7).get_content()
    a.inbox.put(reply_wire)                      # the peer's reply is waiting

    got, td, status = a.exchange(_req(7).get_content(), window=2.0, match_key=match_key)

    assert status == "matched"
    assert got == reply_wire
    assert b.inbox.get_nowait() == _req(7).get_content()   # and the request reached the peer


def test_exchange_times_out_with_no_reply():
    a, _ = _pair()
    a.min_timeout = 0.5
    match_key = V3OpenCodec(addressing="sid").match_spec(_req(7))
    got, td, status = a.exchange(_req(7).get_content(), window=0.3, match_key=match_key)
    assert got is None
    assert status == "timeout"


def test_exchange_ignores_a_foreign_frame_and_keeps_waiting():
    a, _ = _pair()
    a.min_timeout = 0.5
    match_key = V3OpenCodec(addressing="sid").match_spec(_req(7))
    a.inbox.put(_reply(99).get_content())        # a frame for a different session

    got, td, status = a.exchange(_req(7).get_content(), window=0.6, match_key=match_key)

    # the foreign frame was not returned; the window then expired waiting for ours
    assert got is None
    assert status == "timeout"

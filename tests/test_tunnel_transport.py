"""Unit — the split Connector's transport verbs across a tunnel (opaque wire, match at radio).

A Tunnel_connector (logic-holder) forwards transmit / listen / exchange / rf / mac over a
Loopback_link to a Tunnel_interface (bridge) that runs them on a Loopback_connector radio. A
third Loopback_connector is the far peer. This pins the tunnel contract without a UART/socket:
opaque bytes cross unparsed, the match loop runs at the bridge radio, and only the matching
reply comes back — the CPython-testable core of the hardware tunnel.
"""
import threading

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Links.Loopback_link import Loopback_link
from AlLoRa.Codec import V3OpenCodec
from AlLoRa.Packet_v3 import Packet_v3

CONNCFG = {"name": "bridge", "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
           "tx_power": 14, "protocol_version": 3, "addressing": "sid", "timeout_delta": 0.1}


def _tunnel():
    # peer <-air-> bridge_radio ; bridge_radio <-Interface/Link-> tunnel_conn (logic-holder)
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    client_link, bridge_link = Loopback_link.create_pair()
    iface = Tunnel_interface(link=bridge_link)
    iface.setup(bridge_radio, debug=False, config={})
    stop = threading.Event()
    pump = threading.Thread(
        target=lambda: iface.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    conn = Tunnel_connector(link=client_link)
    conn.config(CONNCFG)
    conn.min_timeout = 0.3
    return conn, peer, stop, pump


def _teardown(stop, pump):
    stop.set()
    pump.join(timeout=2)


def _req(sid):
    p = Packet_v3(addressing="sid"); p.set_session(sid); p.ask_metadata(); return p


def _reply(sid):
    p = Packet_v3(addressing="sid"); p.set_session(sid); p.set_ok(); return p


def test_transmit_puts_opaque_bytes_on_the_far_radio():
    conn, peer, stop, pump = _tunnel()
    try:
        assert conn.transmit(b"\x2a\x00hello") is True
        assert peer.recv(1.0) == b"\x2a\x00hello"     # crossed the link + radio unparsed
    finally:
        _teardown(stop, pump)


def test_listen_returns_the_wire_and_a_bridge_measured_td():
    conn, peer, stop, pump = _tunnel()
    try:
        peer.transmit(b"\x2a\x01yo")
        wire, td = conn.listen(1.0)
        assert wire == b"\x2a\x01yo"
        assert td is not None and td >= 0
    finally:
        _teardown(stop, pump)


def test_exchange_matches_at_the_bridge_and_returns_only_the_reply():
    conn, peer, stop, pump = _tunnel()
    try:
        match = V3OpenCodec(addressing="sid").match_spec(_req(0x2a))
        peer.transmit(_reply(0x2a).get_content())        # the reply is waiting at the radio
        got, td, status = conn.exchange(_req(0x2a).get_content(), 2.0, match)
        assert status == "matched"
        assert got == _reply(0x2a).get_content()
        assert peer.recv(1.0) == _req(0x2a).get_content()   # and the request reached the peer
    finally:
        _teardown(stop, pump)


def test_exchange_times_out_when_no_reply_matches():
    conn, peer, stop, pump = _tunnel()
    try:
        match = V3OpenCodec(addressing="sid").match_spec(_req(0x2a))
        peer.transmit(_reply(0x63).get_content())        # a foreign session's frame
        got, td, status = conn.exchange(_req(0x2a).get_content(), 0.6, match)
        assert got is None
        assert status in ("timeout", "exhausted")
    finally:
        _teardown(stop, pump)


def test_get_rf_config_verb_reads_the_bridge_radio():
    conn, peer, stop, pump = _tunnel()
    try:
        assert conn.get_rf_config() == [868, 7, 125, 1, 14]
    finally:
        _teardown(stop, pump)


def test_request_mac_verb_caches_the_bridge_radio_identity():
    conn, peer, stop, pump = _tunnel()
    try:
        assert conn.request_mac() == "b2b2b2b2"
        assert conn.get_mac() == "b2b2b2b2"
    finally:
        _teardown(stop, pump)


def test_change_rf_config_verb_round_trips_and_syncs_local_view():
    conn, peer, stop, pump = _tunnel()
    try:
        # The rf verb reaches the bridge and is accepted; the logic-holder's RF view syncs so
        # Pacing sizes the window from the live sf/bw. (A Loopback radio has no chip to store
        # the change; a real SX126x/SX127x reflects it — verified on hardware.)
        assert conn.change_rf_config(sf=12) is True
        assert conn.sf == 12
    finally:
        _teardown(stop, pump)

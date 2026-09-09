"""Unit — the split Connector's transport verbs across a tunnel (opaque wire, match at radio).

A Tunnel_connector (logic-holder) forwards transmit / listen / exchange / rf / mac over a
Loopback_link to an Adapter (bridge) that runs them on a Loopback_connector radio. A
third Loopback_connector is the far peer. This pins the tunnel contract without a UART/socket:
opaque bytes cross unparsed, the match loop runs at the bridge radio, and only the matching
reply comes back — the CPython-testable core of the hardware tunnel.
"""
import threading

from AlLoRa import tunnel_codec
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Adapters.Adapter import Adapter
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Links.Loopback_link import Loopback_link
from AlLoRa.Codec import V3OpenCodec
from AlLoRa.Packet_v3 import Packet_v3

CONNCFG = {"name": "bridge", "sf": 7, "freq": 868, "bandwidth": 125, "coding_rate": 1,
           "tx_power": 14, "protocol_version": 3, "addressing": "sid", "timeout_delta": 0.1}


def _tunnel():
    # peer <-air-> bridge_radio ; bridge_radio <-Adapter/Link-> tunnel_conn (logic-holder)
    peer, bridge_radio = Loopback_connector.create_pair("a1a1a1a1", "b2b2b2b2")
    bridge_radio.config(CONNCFG)
    client_link, bridge_link = Loopback_link.create_pair()
    bridge = Adapter(bridge_radio, link=bridge_link)
    stop = threading.Event()
    pump = threading.Thread(
        target=lambda: bridge.serve(should_stop=stop.is_set), daemon=True)
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


class _AnswersThePreviousVerb:
    """A bridge that is still finishing the verb the last client asked for.

    It is not silent and it is not broken: it replies, and the reply is well-formed. It just
    belongs to an `exchange` that was already running when this half opened the link, so it
    reaches the RF question as an answer to something else. Flushing cannot prevent it, because
    those bytes had not been written when the flush ran.
    """

    def rpc(self, request, timeout=None):
        return tunnel_codec.encode_exchange_reply(b"\x2a\x00stale", 0.5, "matched")


class _SaysNothing:

    def rpc(self, request, timeout=None):
        return None


def test_get_rf_config_falls_back_when_the_bridge_answers_the_previous_verb():
    conn = Tunnel_connector(link=_AnswersThePreviousVerb())
    conn.config(CONNCFG)
    # The reply decodes to None, not to a config. What comes back is this half's own, which is
    # the same answer the base Connector gives a node whose radio is local.
    assert conn.get_rf_config() == [868, 7, 125, 1, 14]


def test_get_rf_config_falls_back_when_the_bridge_says_nothing():
    conn = Tunnel_connector(link=_SaysNothing())
    conn.config(CONNCFG)
    assert conn.get_rf_config() == [868, 7, 125, 1, 14]


def test_an_unlucky_startup_answer_still_leaves_endpoints_resolvable():
    # The regression this exists for. A node snapshots get_rf_config ONCE and resolves every
    # endpoint against that snapshot forever, so an empty or absent answer at construction is
    # not a missed round trip: it is a node that registers endpoints and can never visit one.
    conn = Tunnel_connector(link=_AnswersThePreviousVerb())
    conn.config(CONNCFG)
    endpoint = Digital_Endpoint(name="T")            # states no RF, like a plain roster entry
    assert endpoint.resolve_rf(conn.get_rf_config()) is True
    assert endpoint.describe_rf() == "868/SF7/BW125/CR1/14dBm"


class _OwesAnOlderAnswer:
    """A bridge holding a reply to a call this half never made.

    That is the state a bridge is left in whenever the previous client stopped waiting: it was
    part-way through a window, and it finishes and writes that reply regardless. Here it arrives
    first, ahead of the answer to the call actually being made.
    """

    def __init__(self):
        self.asked = []

    def rpc(self, request, timeout=None):
        self.asked.append(request)
        return tunnel_codec.encode_exchange_reply(b"\x2a\x00old", 9.9, "matched", req_id=999)

    def read_reply(self, timeout=None):
        wanted = tunnel_codec.decode_request(self.asked[-1])[1]["req_id"]
        return tunnel_codec.encode_get_rf_reply([868, 12, 250, 1, 20], req_id=wanted)


def test_a_reply_to_an_earlier_call_is_discarded_and_the_real_answer_taken():
    link = _OwesAnOlderAnswer()
    conn = Tunnel_connector(link=link)
    conn.config(CONNCFG)
    # Without the id the leftover exchange reply is what `get_rf` would have decoded, and it
    # carries no `rf` at all. With it, that frame is put aside and the next one is read.
    assert conn.get_rf_config() == [868, 12, 250, 1, 20]


def test_the_bridges_own_readings_reach_the_logic_holder():
    conn, peer, stop, pump = _tunnel()
    try:
        # The bridge measures at its radio; this half has no radio to ask. Stub the far side so
        # the numbers are recognisable, then read them back through the ordinary accessors.
        conn._remember_signal(None, None)
        peer.transmit(b"\x2a\x01yo")
        wire, td = conn.listen(1.0)
        assert wire == b"\x2a\x01yo"
        # A Loopback radio measures nothing, so what crosses is its stub. What is pinned here is
        # that the reply carries the pair at all and that listen still answers with two values.
        assert conn.get_rssi() == conn.get_snr() == 0
    finally:
        _teardown(stop, pump)


def test_nothing_heard_leaves_no_reading_rather_than_a_plausible_one():
    conn, peer, stop, pump = _tunnel()
    try:
        wire, td = conn.listen(0.4)          # an empty window: the peer sends nothing
        assert wire is None
        assert conn.get_rssi() is None and conn.get_snr() is None
    finally:
        _teardown(stop, pump)


def test_a_tunnel_reports_no_reading_before_it_has_heard_anything():
    # The stub this replaces returned 0 dBm here, which is a reading no LoRa radio takes, and
    # every Hub on a USB adapter published it as though the link had been measured.
    conn = Tunnel_connector(link=_SaysNothing())
    conn.config(CONNCFG)
    assert conn.get_rssi() is None
    assert conn.get_snr() is None
    assert conn.signal_estimation() == 0      # and nothing downstream raises on the absence


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

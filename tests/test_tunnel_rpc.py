"""Unit — the host<->bridge verb protocol (tunnel_rpc), pure (de)serialization.

The split Connector forwards the transport verbs (transmit / listen / exchange + RF config)
across the tunnel link. tunnel_rpc turns each verb call and its result into opaque bytes and
back, with no I/O, no radio, no codec, no keys — so the Serial/WiFi Link only has to move the
request/reply bytes. The LoRa frame rides inside as an opaque blob (v2 / v3-open / v3-secure
all cross unchanged); these tests pin that the blob and the args survive the round trip.
"""
from AlLoRa.Links import tunnel_rpc as rpc


def test_transmit_request_round_trips_the_opaque_wire():
    wire = bytes([0x2a, 0x01, 0xff, 0x00, 0xde, 0xad])
    verb, args = rpc.decode_request(rpc.encode_transmit(wire))
    assert verb == rpc.TRANSMIT
    assert args["wire"] == wire


def test_listen_request_round_trips_the_window():
    verb, args = rpc.decode_request(rpc.encode_listen(1.75))
    assert verb == rpc.LISTEN
    assert args["window"] == 1.75


def test_exchange_request_round_trips_wire_window_and_match_prefix():
    wire = bytes([0x2a, 0x05, 0x11, 0x22])
    prefix = bytes([0x2a])
    verb, args = rpc.decode_request(rpc.encode_exchange(wire, 2.0, prefix))
    assert verb == rpc.EXCHANGE
    assert args["wire"] == wire
    assert args["window"] == 2.0
    assert args["match_prefix"] == prefix


def test_set_rf_request_round_trips_partial_params():
    # Any of freq/sf/bw/cr/tx may be absent (None) — a partial change must survive.
    verb, args = rpc.decode_request(rpc.encode_set_rf(freq=868, sf=12, bw=None, cr=None, tx=14))
    assert verb == rpc.SET_RF
    assert args["freq"] == 868 and args["sf"] == 12 and args["tx"] == 14
    assert args["bw"] is None and args["cr"] is None


def test_get_rf_and_get_mac_requests():
    assert rpc.decode_request(rpc.encode_get_rf())[0] == rpc.GET_RF
    assert rpc.decode_request(rpc.encode_get_mac())[0] == rpc.GET_MAC


def test_bool_reply_round_trip():
    assert rpc.decode_bool_reply(rpc.encode_bool_reply(True)) is True
    assert rpc.decode_bool_reply(rpc.encode_bool_reply(False)) is False


def test_listen_reply_carries_wire_and_radio_measured_td():
    wire = bytes([0x2a, 0x00, 0x99])
    got, td = rpc.decode_listen_reply(rpc.encode_listen_reply(wire, 0.42))
    assert got == wire
    assert td == 0.42


def test_listen_reply_none_is_a_timeout_not_empty_bytes():
    got, td = rpc.decode_listen_reply(rpc.encode_listen_reply(None, 1.9))
    assert got is None          # a timeout at the radio, distinct from a zero-length frame
    assert td == 1.9


def test_exchange_reply_matched_round_trip():
    wire = bytes([0x2a, 0x00, 0x01])
    got, td, status = rpc.decode_exchange_reply(
        rpc.encode_exchange_reply(wire, 0.6, "matched"))
    assert got == wire and td == 0.6 and status == "matched"


def test_exchange_reply_timeout_carries_no_wire():
    got, td, status = rpc.decode_exchange_reply(
        rpc.encode_exchange_reply(None, 2.0, "timeout"))
    assert got is None and status == "timeout"


def test_get_rf_reply_round_trip():
    assert rpc.decode_get_rf_reply(rpc.encode_get_rf_reply([868, 7, 125, 1, 14])) == [868, 7, 125, 1, 14]


def test_get_mac_reply_round_trip():
    assert rpc.decode_get_mac_reply(rpc.encode_get_mac_reply("9eeff0dc")) == "9eeff0dc"

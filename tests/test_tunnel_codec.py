"""Unit — the host<->bridge verb protocol (tunnel_codec), pure (de)serialization.

The split Connector forwards the transport verbs (transmit / listen / exchange + RF config)
across the tunnel link. tunnel_codec turns each verb call and its result into opaque bytes and
back, with no I/O, no radio, no crypto and no keys, so a Link only has to move the
request/reply bytes whatever medium it runs on. The LoRa frame rides inside as an opaque blob
(v2 / v3-open / v3-secure all cross unchanged); these tests pin that the blob and the args
survive the round trip.
"""
from AlLoRa import tunnel_codec as codec


def test_transmit_request_round_trips_the_opaque_wire():
    wire = bytes([0x2a, 0x01, 0xff, 0x00, 0xde, 0xad])
    verb, args = codec.decode_request(codec.encode_transmit(wire))
    assert verb == codec.TRANSMIT
    assert args["wire"] == wire


def test_listen_request_round_trips_the_window():
    verb, args = codec.decode_request(codec.encode_listen(1.75))
    assert verb == codec.LISTEN
    assert args["window"] == 1.75


def test_exchange_request_round_trips_wire_window_and_match_prefix():
    wire = bytes([0x2a, 0x05, 0x11, 0x22])
    prefix = bytes([0x2a])
    verb, args = codec.decode_request(codec.encode_exchange(wire, 2.0, prefix))
    assert verb == codec.EXCHANGE
    assert args["wire"] == wire
    assert args["window"] == 2.0
    assert args["match_prefix"] == prefix


def test_set_rf_request_round_trips_partial_params():
    # Any of freq/sf/bw/cr/tx may be absent (None) — a partial change must survive.
    verb, args = codec.decode_request(codec.encode_set_rf(freq=868, sf=12, bw=None, cr=None, tx=14))
    assert verb == codec.SET_RF
    assert args["freq"] == 868 and args["sf"] == 12 and args["tx"] == 14
    assert args["bw"] is None and args["cr"] is None


def test_get_rf_and_get_mac_requests():
    assert codec.decode_request(codec.encode_get_rf())[0] == codec.GET_RF
    assert codec.decode_request(codec.encode_get_mac())[0] == codec.GET_MAC


def test_bool_reply_round_trip():
    assert codec.decode_bool_reply(codec.encode_bool_reply(True)) is True
    assert codec.decode_bool_reply(codec.encode_bool_reply(False)) is False


def test_listen_reply_carries_wire_td_and_the_radios_own_readings():
    wire = bytes([0x2a, 0x00, 0x99])
    got, td, rssi, snr = codec.decode_listen_reply(
        codec.encode_listen_reply(wire, 0.42, rssi=-74, snr=9.5))
    assert got == wire
    assert td == 0.42
    assert rssi == -74 and snr == 9.5


def test_listen_reply_none_is_a_timeout_not_empty_bytes():
    got, td, rssi, snr = codec.decode_listen_reply(codec.encode_listen_reply(None, 1.9))
    assert got is None          # a timeout at the radio, distinct from a zero-length frame
    assert td == 1.9
    # Nothing was heard, so there is no reading. Absent, never a plausible-looking number.
    assert rssi is None and snr is None


def test_exchange_reply_matched_round_trip():
    wire = bytes([0x2a, 0x00, 0x01])
    got, td, status, rssi, snr = codec.decode_exchange_reply(
        codec.encode_exchange_reply(wire, 0.6, "matched", rssi=-101, snr=-7.25))
    assert got == wire and td == 0.6 and status == "matched"
    assert rssi == -101 and snr == -7.25


def test_exchange_reply_timeout_carries_no_wire():
    got, td, status, rssi, snr = codec.decode_exchange_reply(
        codec.encode_exchange_reply(None, 2.0, "timeout"))
    assert got is None and status == "timeout"
    assert rssi is None and snr is None


def test_a_zero_snr_survives_as_a_reading_and_is_not_read_as_absent():
    # 0 dB SNR is ordinary for a real radio, so only None may mean "nothing heard". Pinning it
    # because the display workaround downstream keys on RSSI alone for exactly this reason.
    _, _, _, snr = codec.decode_listen_reply(
        codec.encode_listen_reply(b"\x2a", 0.1, rssi=-90, snr=0))
    assert snr == 0


# --- the call id ---

def test_every_request_carries_the_id_it_was_given():
    for frame in (codec.encode_transmit(b"\x2a", req_id=7),
                  codec.encode_listen(1.0, req_id=7),
                  codec.encode_exchange(b"\x2a", 1.0, b"\x2a", req_id=7),
                  codec.encode_set_rf(sf=9, req_id=7),
                  codec.encode_get_rf(req_id=7),
                  codec.encode_get_mac(req_id=7)):
        assert codec.decode_request(frame)[1]["req_id"] == 7


def test_every_reply_echoes_the_id_it_answers():
    for frame in (codec.encode_bool_reply(True, req_id=41),
                  codec.encode_listen_reply(b"\x2a", 0.1, req_id=41),
                  codec.encode_exchange_reply(b"\x2a", 0.1, "matched", req_id=41),
                  codec.encode_get_rf_reply([868, 7, 125, 1, 14], req_id=41),
                  codec.encode_get_mac_reply("9eeff0dc", req_id=41)):
        assert codec.reply_id(frame) == 41


def test_an_id_is_absent_rather_than_null_when_it_is_not_used():
    # This is what lets one half be upgraded before the other: a frame from a half that
    # predates the id must be distinguishable from one that carries an id of None.
    assert b'"i"' not in codec.encode_get_rf()
    assert codec.reply_id(codec.encode_bool_reply(True)) is None
    assert codec.decode_request(codec.encode_get_rf())[1]["req_id"] is None


def test_reply_id_of_something_that_is_not_a_reply_is_none_rather_than_a_raise():
    # It is read before anything is known about the frame, so it has to survive junk: line
    # noise that got past the framing, or a half-written frame from a board that reset.
    assert codec.reply_id(b"not json at all") is None


def test_get_rf_reply_round_trip():
    assert codec.decode_get_rf_reply(codec.encode_get_rf_reply([868, 7, 125, 1, 14])) == [868, 7, 125, 1, 14]


def test_get_mac_reply_round_trip():
    assert codec.decode_get_mac_reply(codec.encode_get_mac_reply("9eeff0dc")) == "9eeff0dc"

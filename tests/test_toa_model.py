"""The time-on-air model, and specifically when it applies low data rate optimization.

The radio turns LDRO on once a symbol lasts longer than 16 ms. That is a function of both
spreading factor and bandwidth, but the model used to test `sf >= 11 and bw == 125`, which
is only the BW125 column of the answer. Every other bandwidth got the wrong airtime: SF12
at BW250 needs LDRO and was priced without it, and at BW62.5 the threshold is crossed as
early as SF10.

Airtime feeds the transmit timeout and the pacing bounds, so a wrong estimate here shortens
a timeout below the frame it is meant to cover.
"""
from AlLoRa.Connectors.Loopback_connector import Loopback_connector

MAC = "a1a1a1a1"


def _toa(sf, bw, payload=50, cr=1):
    return Loopback_connector(MAC).calculate_toa(sf, bw, cr, payload)


def _symbol_time_ms(sf, bw):
    return (2 ** sf) / (bw * 1000.0) * 1000


# (sf, bw, LDRO expected on) — the boundary is symbol time > 16 ms, exclusive.
LDRO_CASES = [
    (7, 125, False), (10, 125, False), (11, 125, True), (12, 125, True),
    (11, 250, False),   # 8.19 ms
    (12, 250, True),    # 16.38 ms, and the case the old rule got wrong
    (12, 500, False),   # 8.19 ms
    (10, 62.5, True),   # 16.38 ms, below SF11 entirely
    (9, 62.5, False),
]


def _toa_with_de(sf, bw, de, payload=50, cr=1):
    """The same closed-form the connector computes, with LDRO forced either way."""
    from math import ceil
    bw_hz = bw * 1000
    t_symbol = (2 ** sf) / bw_hz
    t_preamble = t_symbol * (8 + 4.25)
    payload_bits = 8 * payload - 4 * sf + 28 + 16
    n_payload = 8 + max(0, int(ceil(payload_bits / (4 * (sf - 2 * de))) * (cr / 4.0 + 4)))
    return t_preamble + t_symbol * n_payload


def test_ldro_follows_symbol_time_not_spreading_factor():
    for sf, bw, expect_on in LDRO_CASES:
        actual = _toa(sf, bw)
        on = _toa_with_de(sf, bw, 1)
        off = _toa_with_de(sf, bw, 0)
        want = on if expect_on else off
        assert abs(actual - want) < 1e-9, (
            "SF{} BW{} ({:.2f} ms/symbol): expected LDRO {}, airtime {:.4f} s "
            "matches the other arm".format(
                sf, bw, _symbol_time_ms(sf, bw), "on" if expect_on else "off", actual))


def test_sf12_bw250_is_priced_with_ldro():
    # The regression the old bandwidth-blind rule allowed: 16.38 ms per symbol needs LDRO,
    # but `bw == 125` was false, so the model spent 4*sf bits per symbol instead of
    # 4*(sf-2) and came out optimistic.
    assert _symbol_time_ms(12, 250) > 16
    assert _toa(12, 250) == _toa_with_de(12, 250, 1)
    assert _toa(12, 250) > _toa_with_de(12, 250, 0)


def test_bw125_column_is_unchanged():
    # SF11 and SF12 at BW125 were the one column the old rule got right. The re-derivation
    # must not move them, because the SF7 baseline and every fielded timeout rest on it.
    for sf in (11, 12):
        assert _toa(sf, 125) == _toa_with_de(sf, 125, 1)
    for sf in (7, 8, 9, 10):
        assert _toa(sf, 125) == _toa_with_de(sf, 125, 0)


def test_ldro_only_ever_lengthens_the_estimate():
    # Nothing may get a *shorter* timeout than before, or the fix turns into a new
    # class of truncated transmissions in the field.
    for sf in range(7, 13):
        for bw in (125, 250, 500):
            old_de = 1 if (sf >= 11 and bw == 125) else 0
            assert _toa(sf, bw) >= _toa_with_de(sf, bw, old_de) - 1e-9, (
                "SF{} BW{} got faster than the old model".format(sf, bw))

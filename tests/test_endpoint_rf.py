"""An endpoint that states no radio follows the node that polls it.

A `Digital_Endpoint` carries the RF settings the poller retunes to before listening, which is
how one Hub serves several Edges on different configs. What it used to carry when a config
said nothing was a hardcoded SF7 that appeared in no file anywhere: a Hub whose own LoRa.json
said SF9, holding endpoints registered the ordinary way, retuned itself down to SF7 before
every visit and heard none of them. Nothing warned, and `prepare_connector` reported success
each time, because the retune did exactly what it was told.

So "unstated" now means "wherever the polling node already is", and the settings are read
from a `connector` block spelled exactly as in LoRa.json, so an Edge's own block can be
pasted across unedited. The flat `sf`/`bw`/`cr` keys are the legacy shape and still read.
"""
import json

from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Digital_Endpoint import Digital_Endpoint
from AlLoRa.Nodes.Hub import Hub

HUB_MAC = "b2b2b2b2"
EDGE_MAC = "a1a1a1a1"
FAR_MAC = "c3c3c3c3"


def _write_config(path, result_path, sf=9, bw=125, cr=1, freq=868, tx_power=14):
    config = {
        "name": "hub", "chunk_size": 243, "mesh_mode": False, "short_mac": True,
        "protocol_version": 3, "security_mode": "open", "session_id": 9,
        "debug": False, "result_path": result_path,
        "connector": {"sf": sf, "freq": freq, "bandwidth": bw, "coding_rate": cr,
                      "tx_power": tx_power, "timeout_delta": 0.1, "debug": False},
    }
    with open(path, "w") as f:
        json.dump(config, f)


def _make_hub(tmp_path, nodes=None, **rf):
    """A Hub on sf9 by default, so following it is distinguishable from the old SF7."""
    config = str(tmp_path / "hub.json")
    _write_config(config, str(tmp_path / "results"), **rf)
    kwargs = {}
    if nodes is not None:
        nodes_file = str(tmp_path / "Nodes.json")
        with open(nodes_file, "w") as f:
            json.dump(nodes, f)
        kwargs["nodes_file"] = nodes_file
    else:
        kwargs["nodes_file"] = None
    return Hub(Loopback_connector(HUB_MAC), config_file=config, **kwargs)


def _entry(name, mac, **overrides):
    node = {"name": name, "mac_address": mac, "active": True,
            "asking_frequency": 60, "listening_time": 30}
    node.update(overrides)
    return node


# --- the reported bug ---------------------------------------------------------------------

def test_an_entry_with_no_rf_follows_the_hubs_own_config(tmp_path):
    # The whole deployment case: three Edges at sf9, a Hub at sf9, and Nodes.json entries
    # that say nothing about radio because there is nothing to say.
    hub = _make_hub(tmp_path, nodes=[_entry("a", EDGE_MAC), _entry("b", FAR_MAC)])

    assert [ep.sf for ep in hub.digital_endpoints] == [9, 9]
    assert [ep.bw for ep in hub.digital_endpoints] == [125, 125]


def test_following_the_hub_means_no_retune_at_all(tmp_path):
    # Not just the right number on the endpoint: the radio must be left where it is. This is
    # what failed on the bench, where the retune to SF7 succeeded and the link never formed.
    hub = _make_hub(tmp_path, nodes=[_entry("a", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]

    assert hub.prepare_connector(endpoint) is True
    assert hub.connector.get_rf_config() == [868, 9, 125, 1, 14]


def test_the_keyword_form_follows_the_hub_too(tmp_path):
    # The form the top-level README and every One-Edge example use. It cannot express RF at
    # all, so before this it could only ever mean SF7.
    hub = _make_hub(tmp_path)
    endpoint = Digital_Endpoint(name="edge", mac_address=EDGE_MAC, active=True)
    hub.set_digital_endpoints([endpoint])

    assert endpoint.sf == 9


# --- the connector block, which is LoRa.json's own shape -----------------------------------

def test_a_connector_block_overrides_the_hub(tmp_path):
    # Pasted from the far Edge's own LoRa.json, so it is spelled the way that file spells it.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, connector={
        "sf": 12, "freq": 868, "bandwidth": 250, "coding_rate": 2, "tx_power": 20})])
    endpoint = hub.digital_endpoints[0]

    assert [endpoint.freq, endpoint.sf, endpoint.bw, endpoint.cr, endpoint.tx_power] == \
        [868, 12, 250, 2, 20]


def test_a_partial_block_takes_the_rest_from_the_hub(tmp_path):
    # Per-field, so "this one is far away" is one line rather than a repeat of the whole
    # config. The shipped Nodes.json already wrote partial RF this way.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, connector={"sf": 12})])
    endpoint = hub.digital_endpoints[0]

    assert endpoint.sf == 12, "the stated field wins"
    assert [endpoint.freq, endpoint.bw, endpoint.cr, endpoint.tx_power] == [868, 125, 1, 14]


def test_non_rf_keys_in_a_pasted_block_are_ignored_and_named(tmp_path):
    # A block pasted whole carries the peer's local plumbing. Those describe how that node
    # reaches its own radio, so they mean nothing here; the timeouts in particular are
    # derived from SF on every retune. Dropping them silently is what this records against.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, connector={
        "sf": 12, "min_timeout": 0.5, "max_timeout": 12, "debug": True,
        "serial_port": "/dev/ttyAMA3", "baud": 9600})])
    endpoint = hub.digital_endpoints[0]

    assert endpoint.sf == 12
    assert set(endpoint.rf_skipped) == {"min_timeout", "max_timeout", "debug",
                                        "serial_port", "baud"}


# --- the legacy flat keys ------------------------------------------------------------------

def test_the_legacy_flat_keys_still_work(tmp_path):
    # Deployed Nodes.json files and the student repos are full of these. They keep working.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, sf=12, bw=250, cr=2)])
    endpoint = hub.digital_endpoints[0]

    assert [endpoint.sf, endpoint.bw, endpoint.cr] == [12, 250, 2]
    assert endpoint.tx_power == 14, "an unstated field still follows the Hub"


def test_a_connector_block_wins_over_flat_keys(tmp_path):
    # Both present is a mistake rather than a merge, so one rule: the block decides.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, sf=8, connector={"sf": 12})])

    assert hub.digital_endpoints[0].sf == 12


# --- the trap that rules out resolving against the live radio -------------------------------

def test_the_second_endpoint_follows_the_hub_not_the_first_endpoint(tmp_path):
    # The reason the fallback is a snapshot taken at construction. Visit the far endpoint
    # first and the radio is left on sf12; a fallback read from the live connector would then
    # hand sf12 to every endpoint that states nothing, which is the original bug with a
    # different number. `near` must still be polled on the Hub's own sf9.
    hub = _make_hub(tmp_path, nodes=[_entry("far", FAR_MAC, connector={"sf": 12}),
                                     _entry("near", EDGE_MAC)])
    far, near = hub.digital_endpoints

    assert hub.prepare_connector(far) is True
    assert hub.connector.get_rf_config()[1] == 12, "the radio is now on the far config"

    assert hub.prepare_connector(near) is True
    assert near.sf == 9
    assert hub.connector.get_rf_config()[1] == 9


def test_resolution_is_idempotent(tmp_path):
    # It runs on registration and again on every visit, so a second pass must not re-read
    # anything or move an endpoint that has already been decided.
    hub = _make_hub(tmp_path, nodes=[_entry("a", EDGE_MAC)])
    endpoint = hub.digital_endpoints[0]

    hub.prepare_connector(endpoint)
    hub.connector.change_rf_config(sf=11)
    hub.resolve_endpoint_rf(endpoint)

    assert endpoint.sf == 9, "an already-resolved endpoint must not drift with the radio"


# --- what the operator sees -----------------------------------------------------------------

def test_the_boot_line_names_the_source_of_every_endpoints_rf(tmp_path, capsys):
    # An endpoint on the wrong config and an endpoint correctly following the Hub look
    # identical once resolved, so the source is the part worth printing.
    hub = _make_hub(tmp_path)
    hub.debug = True
    hub.set_digital_endpoints([
        Digital_Endpoint(config=_entry("near", EDGE_MAC)),
        Digital_Endpoint(config=_entry("far", FAR_MAC, connector={"sf": 12, "debug": True})),
    ])

    out = capsys.readouterr().out
    assert "868/SF9/BW125/CR1/14dBm (from node)" in out
    assert "868/SF12/BW125/CR1/14dBm (from connector block)" in out
    assert "ignored non-RF keys: debug" in out


def test_a_resolved_endpoint_is_silent_on_later_visits(tmp_path, capsys):
    # The line belongs at boot, not in the radio loop.
    hub = _make_hub(tmp_path, nodes=[_entry("a", EDGE_MAC)])
    hub.debug = True
    endpoint = hub.digital_endpoints[0]
    capsys.readouterr()

    hub.resolve_endpoint_rf(endpoint)

    assert "from node" not in capsys.readouterr().out

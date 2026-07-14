"""Tunnel_connector identity — a v2 tunnel Collector's on-air address is the *bridge's* MAC.

v2 reply-matching checks that a reply is addressed to *me*; over a tunnel "me" is the bridge
radio, not the host running the logic. The codec is built at config time with a placeholder
MAC, so the tunnel must fetch the bridge's real MAC and rebuild the codec — otherwise every v2
reply looks unaddressed-to-me and the transfer stalls. v3 (sid-addressed) is MAC-independent
and must NOT pay that round trip.
"""
import struct
import threading

from AlLoRa.Codec import V2Codec, V3OpenCodec
from AlLoRa.Connectors.Loopback_connector import Loopback_connector
from AlLoRa.Connectors.Tunnel_connector import Tunnel_connector
from AlLoRa.Interfaces.Tunnel_interface import Tunnel_interface
from AlLoRa.Links.Loopback_link import Loopback_link

BRIDGE_MAC = "b2b2b2b2"
PEER_MAC = "a1a1a1a1"


def _bridge(mac, config):
    radio = Loopback_connector(mac)
    radio.config(config)
    client_link, bridge_link = Loopback_link.create_pair()
    iface = Tunnel_interface(link=bridge_link)
    iface.setup(radio, debug=False, config={})
    stop = threading.Event()
    pump = threading.Thread(target=lambda: iface.serve(should_stop=stop.is_set), daemon=True)
    pump.start()
    return client_link, stop, pump


def test_v2_tunnel_adopts_the_bridge_mac_and_rebuilds_the_codec():
    config = {"sf": 7, "freq": 868, "protocol_version": 2, "addressing": "mac",
              "short_mac": True, "debug": False}
    client_link, stop, pump = _bridge(BRIDGE_MAC, config)
    try:
        conn = Tunnel_connector(link=client_link)
        conn.config(config)

        assert conn.get_mac() == BRIDGE_MAC, "tunnel did not adopt the bridge radio's MAC"
        assert isinstance(conn.codec, V2Codec)

        # A reply is 'mine' when addressed to the bridge MAC; the rebuilt codec's match prefix
        # must end in the bridge MAC bytes, not the placeholder 00000000.
        class _Req:
            def get_destination(self):
                return PEER_MAC
        spec = conn.codec.match_spec(_Req())
        assert spec.wire_prefix().endswith(struct.pack("I", int(BRIDGE_MAC, 16)))
    finally:
        stop.set()
        pump.join(timeout=2)


def test_v3_open_tunnel_does_not_need_or_fetch_a_mac():
    config = {"sf": 7, "freq": 868, "protocol_version": 3, "addressing": "sid",
              "short_mac": True, "debug": False}
    client_link, stop, pump = _bridge(BRIDGE_MAC, config)
    try:
        conn = Tunnel_connector(link=client_link)
        conn.config(config)
        # sid addressing is MAC-independent: the codec is v3-open and the MAC stays the
        # placeholder (no wasted round trip to the bridge).
        assert isinstance(conn.codec, V3OpenCodec)
        assert conn.get_mac() == "00000000"
    finally:
        stop.set()
        pump.join(timeout=2)

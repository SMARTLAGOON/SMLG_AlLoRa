"""Tunnel_interface — the bridge (Adapter) half of a split Connector.

The Adapter holds the radio but no protocol logic: it reads a transport-verb request off the
link, runs that verb on its real Connector, and writes the result back. It never parses the
LoRa wire, never holds a session key — it moves opaque bytes and, for `exchange`, matches
replies on the cleartext prefix the logic-holder sent down. That is what lets a single dumb
bridge serve v2, v3-open and v3-secure alike, and what "Adapter is the server half of a split
Connector, not a node type" means in practice.
"""
from AlLoRa.Interfaces.Interface import Interface
from AlLoRa.Links import tunnel_rpc
from AlLoRa.utils.debug_utils import print


class _PrefixMatch:
    """Keyless reply matcher for the bridge radio's `exchange`: a reply is ours when it starts
    with the prefix the logic-holder sent down (a sid byte, a device_id[:4] token, or the
    src+dst MAC bytes). No codec, no keys — the codec on the logic-holder has the final say."""

    def __init__(self, prefix):
        self._prefix = bytes(prefix) if prefix else b""

    def matches_wire(self, wire):
        n = len(self._prefix)
        return n > 0 and len(wire) >= n and wire[:n] == self._prefix


class Tunnel_interface(Interface):

    def __init__(self, link=None):
        super().__init__()
        self.link = link

    def serve(self, should_stop=None):
        """Pump verb requests until `should_stop()` says stop (or forever). Each request runs
        one radio verb and writes exactly one reply, so the client's blocked rpc always wakes."""
        while should_stop is None or not should_stop():
            self.handle_one(timeout=0.5)

    def handle_one(self, timeout=None):
        request = self.link.read_request(timeout=timeout)
        if request is None:
            return False
        try:
            verb, args = tunnel_rpc.decode_request(request)
            self._dispatch(verb, args)
        except Exception as e:
            if self.debug:
                print("Tunnel_interface error: {}".format(e))
            # Reply so a client rpc never blocks forever on a malformed/failed request.
            self.link.write_reply(tunnel_rpc.encode_bool_reply(False))
        return True

    def _dispatch(self, verb, args):
        if verb == tunnel_rpc.TRANSMIT:
            ok = self.connector.transmit(args["wire"])
            self.link.write_reply(tunnel_rpc.encode_bool_reply(ok))
        elif verb == tunnel_rpc.LISTEN:
            wire, td = self.connector.listen(args["window"])
            self.link.write_reply(tunnel_rpc.encode_listen_reply(wire, td))
        elif verb == tunnel_rpc.EXCHANGE:
            reply, td, status = self.connector.exchange(
                args["wire"], args["window"], _PrefixMatch(args["match_prefix"]))
            self.link.write_reply(tunnel_rpc.encode_exchange_reply(reply, td, status))
        elif verb == tunnel_rpc.SET_RF:
            ok = self.connector.change_rf_config(
                frequency=args.get("freq"), sf=args.get("sf"), bw=args.get("bw"),
                cr=args.get("cr"), tx_power=args.get("tx"))
            self.link.write_reply(tunnel_rpc.encode_bool_reply(bool(ok)))
        elif verb == tunnel_rpc.GET_RF:
            self.link.write_reply(tunnel_rpc.encode_get_rf_reply(self.connector.get_rf_config()))
        elif verb == tunnel_rpc.GET_MAC:
            self.link.write_reply(tunnel_rpc.encode_get_mac_reply(self.connector.get_mac()))
        else:
            self.link.write_reply(tunnel_rpc.encode_bool_reply(False))

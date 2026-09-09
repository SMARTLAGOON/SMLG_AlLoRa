"""Adapter: the bridge half of a split Connector, and the thing a bridge board boots.

When the radio sits on a different board from the logic (a Raspberry Pi has no LoRa), the
Connector is split in two halves: the node holds the near half (WiFi_connector,
Serial_connector) and the bridge board runs this far half, which drives the real radio
Connector. The halves exchange transport verbs over a Link, a dumb byte pipe.

The bridge holds no protocol logic, no files, no sessions and no keys: it reads a
transport-verb request off the link, runs that verb on its radio, and writes the result back.
It never parses the LoRa wire and, for `exchange`, matches replies on the cleartext prefix the
logic-holder sent down. That is what lets one dumb bridge serve v2, v3-open and v3-secure
alike, and it is why an Adapter is not a node: the node-type set is exactly {Edge, Hub}. It
holds a Status so a bridge board can still drive a screen or a logger, which needs no protocol.

Two ways in, because a bridge is used two ways:

  * `Adapter(radio, link=link)` wires the two halves directly. Nothing is read from disk.
  * `Serial_adapter(radio)` / `WiFi_adapter(radio)` / `USB_adapter(radio)` boot from a config
    file: they configure the radio from its `connector` block and build their own Link from the
    `adapter` block. `USB_adapter` is the one for a board reached over its own USB socket, and
    it is also the one that cannot print: its console is the link, so it redirects the library's
    debug output before it boots.
"""
import gc

from AlLoRa.Connectors.Connector import Connector
from AlLoRa import tunnel_codec
from AlLoRa.Status import Status
from AlLoRa.utils.time_utils import current_time_ms as time, ticks_add, ticks_diff, sleep
from AlLoRa.utils.debug_utils import print

from json import loads


class _PrefixMatch:
    """Keyless reply matcher for the bridge radio's `exchange`: a reply is ours when it starts
    with the prefix the logic-holder sent down (a sid byte, a device_id[:4] token, or the
    src+dst MAC bytes). No codec, no keys: the codec on the logic-holder has the final say."""

    def __init__(self, prefix):
        self._prefix = bytes(prefix) if prefix else b""

    def matches_wire(self, wire):
        n = len(self._prefix)
        return n > 0 and len(wire) >= n and wire[:n] == self._prefix


class Adapter:

    # Yielded only when there was nothing to serve, or when the link raised. Both links in the
    # tree already block or poll-with-sleep inside read_request, so this buys them nothing; it
    # is here so a Link that returns immediately without yielding cannot spin the board, since
    # on-device CPU is a design invariant. It must never fire between back-to-back verbs: on
    # the WiFi tunnel that cost 100 ms per verb, about 15% of a 1 KB transfer's wall clock.
    IDLE_SLEEP = 0.1

    def __init__(self, connector: Connector = None, config_file=None, link=None, debug=False):
        self.connector = connector
        self.config_file = config_file
        self.link = link
        self.debug = debug
        self.name = "A"

        # The observability surface. A bridge runs no protocol, so it reports only what it can
        # actually see: the radio's identity and the signal of the last frame it handled.
        self.status = Status()
        self.status["RSSI"] = "-"
        self.status["SNR"] = "-"

        if self.config_file is not None:
            self.boot()

    # --- booting a board -------------------------------------------------------------------

    def boot(self):
        """Read the config file, configure the radio from it, then build the medium's Link.

        The radio gets the same derived block a node would give it (the framing keys ride along
        with the RF values), so a bridge board's radio behaves identically to how it did when a
        Node owned this config step.
        """
        with open(self.config_file, "r") as f:
            config = loads(f.read())

        self.name = config.get('name', "A")
        self.debug = config.get('debug', False)

        connector_config = config.get('connector', None) or {}
        connector_config['mesh_mode'] = config.get('mesh_mode', False)
        connector_config['short_mac'] = config.get('short_mac', False)
        connector_config['protocol_version'] = config.get('protocol_version', 2)
        connector_config['addressing'] = \
            'sid' if connector_config['protocol_version'] >= 3 else 'mac'
        self.connector.config(connector_config)
        self.status["MAC"] = self.connector.get_mac()[-8:]
        # The radio's own settings: a bridge can see these, so a board with a screen keeps
        # showing them. The transfer fields (file, chunk, retransmissions) stay absent because
        # they belong to the logic-holder, which is the half that runs the protocol.
        self.status["Freq"] = self.connector.frequency
        self.status["SF"] = self.connector.sf
        self.status["BW"] = self.connector.bw
        self.status["CR"] = self.connector.cr
        self.status["TX_P"] = self.connector.tx_power
        print(self.name, ":", self.status["MAC"])

        # `interface` is the pre-Adapters spelling of this block; it is still read so a fielded
        # bridge board boots unchanged from the config file already on its flash.
        self.setup_link(config.get('adapter', config.get('interface', None)) or {})

    def setup_link(self, config):
        """Build the concrete Link for this medium. The base has no medium: it is either handed
        a link directly, or subclassed by one that has (Serial_adapter, WiFi_adapter)."""
        pass

    # --- running ---------------------------------------------------------------------------

    def run(self, timeout=None):
        """The bridge's main loop: serve the channel on the node's orders, forever.

        `timeout` is in seconds; None runs forever, which is what a deployed main.py wants.
        Every served request refreshes the signal readings and notifies subscribers, so a
        bridge with a screen shows the link as it works.
        """
        end_time = None if timeout is None else ticks_add(time(), timeout * 1000)
        while end_time is None or ticks_diff(end_time, time()) > 0:
            try:
                if self.handle_one(timeout=0.5):
                    self.status["RSSI"] = self.connector.get_rssi()
                    self.status["SNR"] = self.connector.get_snr()
                    self.status.notify()
                    gc.collect()
                else:
                    sleep(self.IDLE_SLEEP)
            except KeyboardInterrupt:
                if self.debug:
                    print("THREAD_EXIT")
                return
            except Exception as e:
                if self.debug:
                    print("Error in Adapter: {}".format(e))
                # A link that raises rather than returning None (a dead UART, an unplugged
                # bridge) would otherwise retry with no pause at all.
                sleep(self.IDLE_SLEEP)

    def serve(self, should_stop=None):
        """Pump verb requests until `should_stop()` says stop (or forever). Each request runs
        one radio verb and writes exactly one reply, so the client's blocked rpc always wakes.

        This is the bare pump, without run()'s status refresh and idle pause: it is what a
        caller driving the bridge from its own loop wants.
        """
        while should_stop is None or not should_stop():
            self.handle_one(timeout=0.5)

    def handle_one(self, timeout=None):
        """One verb request in, one reply out. Returns whether a request was served."""
        request = self.link.read_request(timeout=timeout)
        if request is None:
            return False
        # Read outside the try so a request that fails to dispatch is still answered under its
        # own number. An error reply the client cannot place is one it has to discard, which
        # would leave it waiting out the whole deadline for an answer that already came.
        req_id = tunnel_codec.reply_id(request)
        try:
            verb, args = tunnel_codec.decode_request(request)
            self._dispatch(verb, args)
        except Exception as e:
            if self.debug:
                print("Adapter error: {}".format(e))
            # Reply so a client rpc never blocks forever on a malformed/failed request.
            self.link.write_reply(tunnel_codec.encode_bool_reply(False, req_id=req_id))
        return True

    def _signal(self, received):
        """What the radio measured on the frame it just took, or (None, None) if it took none.

        Asked here, immediately after the verb, because these are properties of the last frame
        received and the next verb overwrites them. A window that heard nothing has nothing to
        report, and saying so is the point: a number invented for that case is indistinguishable
        downstream from a node that is genuinely being heard.
        """
        if not received:
            return None, None
        return self.connector.get_rssi(), self.connector.get_snr()

    def _dispatch(self, verb, args):
        req_id = args.get("req_id")
        if verb == tunnel_codec.TRANSMIT:
            ok = self.connector.transmit(args["wire"])
            self.link.write_reply(tunnel_codec.encode_bool_reply(ok, req_id=req_id))
        elif verb == tunnel_codec.LISTEN:
            wire, td = self.connector.listen(args["window"])
            rssi, snr = self._signal(wire)
            self.link.write_reply(
                tunnel_codec.encode_listen_reply(wire, td, rssi=rssi, snr=snr, req_id=req_id))
        elif verb == tunnel_codec.EXCHANGE:
            reply, td, status = self.connector.exchange(
                args["wire"], args["window"], _PrefixMatch(args["match_prefix"]))
            rssi, snr = self._signal(reply)
            self.link.write_reply(
                tunnel_codec.encode_exchange_reply(reply, td, status, rssi=rssi, snr=snr,
                                                   req_id=req_id))
        elif verb == tunnel_codec.SET_RF:
            ok = self.connector.change_rf_config(
                frequency=args.get("freq"), sf=args.get("sf"), bw=args.get("bw"),
                cr=args.get("cr"), tx_power=args.get("tx"))
            self.link.write_reply(tunnel_codec.encode_bool_reply(bool(ok), req_id=req_id))
        elif verb == tunnel_codec.GET_RF:
            self.link.write_reply(
                tunnel_codec.encode_get_rf_reply(self.connector.get_rf_config(), req_id=req_id))
        elif verb == tunnel_codec.GET_MAC:
            self.link.write_reply(
                tunnel_codec.encode_get_mac_reply(self.connector.get_mac(), req_id=req_id))
        else:
            self.link.write_reply(tunnel_codec.encode_bool_reply(False, req_id=req_id))

    # --- who is watching -------------------------------------------------------------------

    def register_subscriber(self, subscriber):
        self.status.register(subscriber)

    def unregister_subscriber(self, subscriber):
        self.status.unregister(subscriber)

    def notify_subscribers(self):
        self.status.notify()

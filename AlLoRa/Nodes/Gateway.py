"""Deprecated name: `Gateway` is now `Hub` (named by placement, not by what it gateways).

`Gateway` used to be the multi-endpoint collector and `Requester` the 1:1 one, two classes for
one node placed the same way. That split is gone: a Hub holds one or more endpoints and owns
the visit loop itself, so nothing is left here but the pre-v3 argument order, kept so fielded
main.py files construct unchanged. New code says `Hub(...).run()`.
"""
from AlLoRa.Nodes.Requester import Requester


class Gateway(Requester):

    def __init__(self, connector=None, config_file="LoRa.json", debug_hops=False,
                 NEXT_ACTION_TIME_SLEEP=0.1, nodes_file="Nodes.json", data_sink=None,
                 **kwargs):
        # NEXT_ACTION_TIME_SLEEP is accepted for legacy callers but ignored, as it has been
        # since v2.0: the adaptive sleep controller owns the gap. Everything else is
        # forwarded rather than enumerated, which is how the visit-budget knobs went missing
        # once already and left the fielded node pinned to defaults it could not override.
        super().__init__(connector, config_file, debug_hops=debug_hops,
                         nodes_file=nodes_file, data_sink=data_sink, **kwargs)

"""Deprecated name: `Requester` is now `Hub` (named by placement, not by who requests).

The class survives as an alias so existing collectors, gateways and examples keep importing
and running unchanged; new code and docs say Hub. The whole machinery lives on `Node`, which
a Hub presets to the "collector" home role.
"""
from AlLoRa.Nodes.Hub import Hub


class Requester(Hub):

    def __init__(self, connector=None, config_file="LoRa.json",
                 NEXT_ACTION_TIME_SLEEP=None, **kwargs):
        # The legacy ctor accepted NEXT_ACTION_TIME_SLEEP, and v2.0 already ignored
        # it: the adaptive sleep controller (now Pacing) replaced the fixed
        # inter-request gap. The shim keeps accepting (and ignoring) it so fielded
        # main.py files construct unchanged; `Hub` itself no longer takes it.
        super().__init__(connector, config_file, **kwargs)

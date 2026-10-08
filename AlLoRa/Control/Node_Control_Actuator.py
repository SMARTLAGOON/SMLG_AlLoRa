import sys

from AlLoRa.Control.Control_Actuator import Control_Actuator
from AlLoRa.Control.control_types import RF_CONFIG, RESET
from AlLoRa.utils.json_utils import json
from AlLoRa.utils.debug_utils import print


class Node_Control_Actuator(Control_Actuator):
    """The on-node actuator the control-root verifier hands a *verified* control artifact to.

    It never acts in apply(): apply() runs inside consume(), which fires before the transfer's
    final-OK reaches the air, so switching the radio or resetting here would break the
    acknowledgement (a stale config the peer never hears, or a reboot loop). Instead it queues
    a deferred action on the node; the Edge drains it after the pull completes, once the
    final-OK is out. This mirrors the serve path, which replies on the old config and only then
    switches.
    """

    handles = (RF_CONFIG, RESET)

    def __init__(self, node, reset_fn=None):
        self.node = node
        # None -> the default for where this runs, resolved at drain time: machine.reset() on a
        # board, an exit of the AlLoRa program on a host (see _default_reset). A deployment that
        # wants a full reboot of its Pi, or a reset of its radio adapter, passes its own.
        self._reset_fn = reset_fn

    def apply(self, control_type, payload):
        if control_type == RF_CONFIG:
            try:
                # Decode bytes -> str before parsing: some MicroPython ujson builds only accept a
                # str, and silently dropping a *valid* config on-device is the worst failure here.
                text = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload
                cfg = json.loads(text)
            except Exception:
                # A validly-signed but unparseable payload is a backend bug, not a transient
                # failure: drop it (do NOT raise). Raising would re-pull the identical bad bytes
                # forever and withhold the final-OK. Same anti-loop rule the gate's reject follows.
                return self._drop("RF_CONFIG payload is not valid JSON")
            if not isinstance(cfg, dict):
                return self._drop("RF_CONFIG payload is not a JSON object")
            change_rf_config = self.node.change_rf_config
            # Defer: the Edge drains this after the final-OK is on the air. Capture the bound
            # method + cfg (not self) so the queued thunk does not pin the whole actuator.
            self.node.queue_control_action(lambda: change_rf_config(cfg))
        elif control_type == RESET:
            reset_fn = self._reset_fn

            def _reset():
                (reset_fn or _default_reset)()

            # Defer: reboot only after the final-OK is out, or the Hub never hears completion,
            # re-grants the downlink, and the Edge reboots into the same RESET forever.
            self.node.queue_control_action(_reset)

    def _drop(self, reason):
        # A dropped-but-verified artifact is a rare, ops-relevant event (a genuine, signed
        # command the node cannot act on): log it. Returns None so apply() just falls through.
        print("Node_Control_Actuator: dropped a verified control artifact ({})".format(reason))
        return None


def _default_reset():
    # RESET always means AlLoRa's own node. On a board that is the board. On a host such as a
    # Pi it is the AlLoRa program, which exits with a failure status so the service running it
    # starts it again: a clean exit would leave a service set to restart on failure stopped.
    if sys.implementation.name == "micropython":
        import machine   # device-only; imported here so a host never needs it
        machine.reset()
    sys.exit("Node_Control_Actuator: RESET received, exiting for the service to restart AlLoRa")

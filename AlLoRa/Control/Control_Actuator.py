"""Control_Actuator: where a verified command becomes an effect on the device.

The control path has two halves, deliberately separate objects. `Control_Root_DataSink` is the
verify gate: it receives the reassembled downlink file, checks the envelope and the signature
against the control root, and passes on only what is authentic and addressed to this node. What
it passes to is an actuator, whose job is the effect itself: switch the radio, reboot, later
apply a model or an image.

Two reasons for the split. The gate is generic, holding all the crypto and no device knowledge,
so it is reused unchanged on any node; the actuator is device-specific and owns the one thing
the gate cannot know, which is *when it is safe to act*. Acting inside apply() would break the
acknowledgement the node is still in the middle of sending, so the node-side actuator queues a
deferred action and the run loop drains it once the final-OK is on the air.

Subclass this to give a deployment its own effects.
"""


class Control_Actuator:
    """The actuator boundary the verifier hands a *verified* control artifact to."""

    def apply(self, control_type, payload):
        raise NotImplementedError(
            "Control_Actuator subclasses must implement apply(control_type, payload)")

"""Carries out a control command after `Control_Root_DataSink` has verified it.

Do not act inside `apply()`, because the node is still sending its acknowledgement. Queue the
action instead, and the run loop carries it out after the final OK. Subclass this to add your own
actions.
"""


class Control_Actuator:
    """The actuator boundary the verifier hands a *verified* control artifact to."""

    def apply(self, control_type, payload):
        raise NotImplementedError(
            "Control_Actuator subclasses must implement apply(control_type, payload)")

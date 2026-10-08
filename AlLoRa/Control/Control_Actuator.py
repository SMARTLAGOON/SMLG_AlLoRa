"""Carries out a control command after `Control_Root_DataSink` has verified it.

Do not act inside `apply()`, because the node is still sending its acknowledgement. Queue the
action instead, and the run loop carries it out after the final OK. Subclass this to add your own
actions, and list in `handles` the control types your `apply()` acts on.
"""


class Control_Actuator:
    """The actuator boundary the verifier hands a *verified* control artifact to."""

    # The control types this actuator acts on. The gate forwards a verified artifact only when
    # its type is one the library knows and is listed here, so a type nothing handles is
    # dropped rather than acknowledged and ignored. Listing a type the library does not know
    # forwards nothing: an actuator can narrow what reaches it, never widen it.
    handles = ()

    def apply(self, control_type, payload):
        raise NotImplementedError(
            "Control_Actuator subclasses must implement apply(control_type, payload)")

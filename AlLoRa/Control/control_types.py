"""The control artifact type vocabulary: what a signed downlink command asks for.

A closed enum, shared by the two halves of the control path and owned by neither: the verify
gate reads the type to decide whether an actuator exists for it, and the actuator dispatches on
it to pick the effect. The byte travels inside the *signed* region of the envelope, so a
purpose cannot be relabeled in flight.

Values are wire constants: append, never renumber.
"""
RF_CONFIG = 1
RESET = 2
MODEL = 3
OTA = 4

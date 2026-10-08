"""The kinds of control command (radio config, reset, model, OTA, custom), as numbers.

These numbers are sent over the air. Add new ones at the end and never change existing ones.
"""
RF_CONFIG = 1   # change this node's radio settings, with a trial
RESET = 2       # restart AlLoRa's own node: the board, or the program on a host
MODEL = 3       # an AiLoRa model: changes how this node picks its own radio settings
OTA = 4         # a firmware or package update of AlLoRa itself
# Signed bytes for whatever runs beside AlLoRa (a camera board, a program on the same Pi).
# AlLoRa checks and hands them over and never reads them: the deployment defines the content.
CUSTOM = 5

# The in-band namespace bit, for a control command carried by the link itself instead of by a
# signed downlink artifact. Such a command rides a CTRL frame whose 1-byte payload prefix is
# this bit ORed with the control type, so a receiver does one mask to pick the namespace and
# one to read the type back verbatim. It keeps control commands disjoint from the handshake
# kinds, which stay at 0x00 to 0x7F: before this, an unknown handshake kind and an unknown
# control type were indistinguishable at the drop, so neither could be logged for what it was.
#
# The cost, stated rather than buried: bit 7 is spent permanently, capping each namespace at
# 128 values. Both vocabularies are closed and hold a handful each, so it is not a real constraint,
# but it is irreversible once a frame has existed on a board.
#
# 0x80 itself names no control type, since the enum starts at 1, which leaves it free as the
# sentinel for a malformed frame.
IN_BAND = 0x80

# Every type this build knows. A receiver checks membership rather than a range, so a byte
# that is merely inside the namespace does not read as a purpose: an unimplemented type and a
# reserved bit that has since been given a meaning both land outside and are dropped.
KNOWN = (RF_CONFIG, RESET, MODEL, OTA, CUSTOM)

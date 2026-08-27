"""The AlLoRa provisioning wizard: flash a board, provision it, and prove the pair works.

Lives inside the library rather than beside it because it has to run the library's own crypto.
It mints P-256 control roots and derives `device_id` fingerprints on the operator's machine
that must match, byte for byte, what the board computes; vendoring a second copy of that code
would drift silently and surface as a node that will not handshake.

CPython only, and never frozen into firmware: the manifest freezes `AlLoRa/`, so nothing here
reaches a board. `esptool` and `mpremote` are shelled out to, not imported, so the base install
needs neither.
"""

"""The AlLoRa provisioning wizard: flash a board, provision it, and check that the pair works.

It lives in the library repo because it runs the library's own crypto: the keys and `device_id`s
it makes on the computer must match what the board computes. It runs on a computer only and is
never put on a board. It calls `esptool` and `mpremote` as programs, so neither is a dependency.
"""

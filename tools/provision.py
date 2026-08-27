#!/usr/bin/env python3
"""The AlLoRa provisioning wizard.

    python3 tools/provision.py fleet-init
    python3 tools/provision.py edge --firmware AlLoRa-t3s3-sx127x-firmware.bin
    python3 tools/provision.py hub  --firmware AlLoRa-t3s3-sx127x-firmware.bin
    python3 tools/provision.py verify --edge-port /dev/cu.usbmodem1101 \
                                      --hub-port  /dev/cu.usbmodem2101

Every command takes `--json`. CPython only, and never frozen into firmware: the manifest
freezes `AlLoRa/`, so nothing under `tools/` reaches a board.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.allora_provision.cli import main      # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

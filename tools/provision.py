#!/usr/bin/env python3
"""The AlLoRa provisioning wizard. Commands and examples: tools/README.md, or `--help`.

    python3 tools/provision.py setup

Every command takes `--json`. It runs on a computer only and is never put on a board.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.allora_provision.cli import main      # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

import os
import sys

# Make the package importable when running the test suite from the repo root
# (pytest's default prepend import mode only inserts the test file's directory).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

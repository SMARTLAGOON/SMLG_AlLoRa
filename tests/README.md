# Tests

These are the host tests: they run the library on a computer, under CPython, with no boards and
no radios. A change that breaks a transfer, a handshake or a config file shows up here in minutes,
before anyone flashes a board.

## Running them

GitHub runs them on Python 3.10. From the repository root, install the tools once:

```bash
python3 -m pip install pytest pytest-xdist cryptography "mpy-cross==1.24.1.*" mpremote esptool pyserial
```

Then run the whole suite on four workers:

```bash
python3 -m pytest -q -n 4
```

It takes about eight minutes. Keep it at four workers: many tests run real transfers against the
clock, and with more workers they starve each other and time out. One worker takes about twenty
minutes.

To run one file, or the tests whose names match a word:

```bash
python3 -m pytest -q tests/test_codec.py
python3 -m pytest -q -k handshake
```

Run from the repository root: `conftest.py` there puts the library on the import path.

## What the tools are for

- `pytest-xdist` gives the `-n 4` option.
- `cryptography` is the AES the secure tests run on. Without it they fail, since a computer has no
  other AES backend.
- `mpy-cross` is MicroPython's compiler. It must be the version the firmware is built on (the
  `micropython_ref` in `.github/workflows/build-firmware.yml`); with no compiler, or the wrong
  version, that one test skips and says which to install.
- `mpremote`, `esptool` and `pyserial` are for the provisioning wizard's tests and the serial
  examples.

A few tests run the library as tagged v2.0.0, so they need the repository's history and its tags.
In a shallow clone, or one without tags, they fail with a message saying what to fetch:
`git fetch --tags`, or `git fetch --unshallow --tags` if the clone is shallow.

## How they work

A transfer needs two nodes and something between them. The tests replace the radio:

- `Loopback_connector` (in `AlLoRa/Connectors/`) is an in-memory radio. An Edge and a Hub run in
  one process, one of them on a background thread, and frames pass through a queue. It can also drop frames on
  purpose, to test the retries.
- `fake_sx127x.py` is a fake SX1276 chip at the register level, so the real SX127x driver can be
  tested without a board.
- `v2_tag.py` and `v2_tag_peer.py` run the library as tagged v2.0.0, in a second process, so the
  tests can check that the current library still talks to a v2 node.
- `delegation_drive.py` runs a downlink until it finishes, instead of for a fixed number of
  seconds, so a slow machine takes longer rather than failing.

Some tests check things a board would only show much later:

- `test_micropython_compiles.py` compiles every library file with MicroPython's compiler, which
  rejects some syntax CPython accepts.
- `test_micropython_portability.py` looks for calls that exist on CPython but not on MicroPython
  (the traps are listed in [`firmware/README.md`](../firmware/README.md)).
- `test_readme_refs.py` checks that the main README's links, the methods it names and the keys in
  its config examples still exist in the code.

## When they run

GitHub runs the suite on every push and pull request to `main` and `v3`
(`.github/workflows/tests.yml`), and again before a release is published
(`.github/workflows/release.yml`).

## What a green run does not prove

The suite runs on CPython, which has more than MicroPython has. A call that works here can still
fail on the ESP32, and timing on a real radio is not the timing of a queue. After a change to the
radio, the secure path or anything timing-related, run it on two boards as well.

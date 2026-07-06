#!/usr/bin/env python3
"""Log a board's serial session to a timestamped file (and echo it to the terminal).

Run ONE per device, in its own terminal tab:

    python3 capture_serial.py /dev/cu.usbmodemA source.log
    python3 capture_serial.py /dev/cu.usbmodemB collector.log --seconds 90

Every line is prefixed with seconds-since-start, so the two logs can be aligned
to see who transmits / who waits.

Stopping:
  * Ctrl-C stops it cleanly within ~0.5 s (a signal handler flips a flag — this
    works even while the serial output is flooding).
  * Press Ctrl-C twice, or Ctrl-\ (SIGQUIT), to force-quit immediately.
  * --seconds N  auto-stops after N seconds (0 = run until Ctrl-C, the default).

Notes:
  * Only ONE program may hold a serial port at a time — close any screen / picocom /
    ampy on that port first, or you'll get "resource busy".
  * Press the board's RESET button after starting, to capture from the boot banner.
  * Needs pyserial (already present if you use ampy):  pip install pyserial
"""
import argparse
import signal
import time

import serial  # pyserial

_running = True
_hits = 0


def _stop(signum, frame):
    # Flag-based stop: the read loop has a short timeout, so it notices this
    # within one timeout tick even while the terminal is flooding. A second
    # Ctrl-C (or Ctrl-\) force-quits.
    global _running, _hits
    _running = False
    _hits += 1
    if _hits >= 2:
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser(description="Timestamped serial logger.")
    ap.add_argument("port", help="e.g. /dev/cu.usbmodem2101")
    ap.add_argument("logfile", help="output file, e.g. source.log")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="auto-stop after N seconds (0 = until Ctrl-C)")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    ser = serial.Serial(args.port, args.baud, timeout=0.5)
    t0 = time.time()
    print("# capturing %s @ %d -> %s   (Ctrl-C to stop)" % (args.port, args.baud, args.logfile))

    with open(args.logfile, "w") as f:
        f.write("# %s @ %d, started %s\n" % (args.port, args.baud, time.strftime("%Y-%m-%d %H:%M:%S")))
        f.flush()
        while _running:
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
            raw = ser.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            stamp = "[%8.3f] %s" % (time.time() - t0, text)
            print(stamp)
            f.write(stamp + "\n")
            f.flush()

    ser.close()
    print("\n# stopped (%.1fs captured)" % (time.time() - t0))


if __name__ == "__main__":
    main()

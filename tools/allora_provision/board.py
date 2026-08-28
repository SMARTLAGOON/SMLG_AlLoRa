"""Talking to a T3S3 over the wire, with the bench's hard-won gotchas encoded rather than
documented.

The single fact that drives almost everything here is that the ESP32-S3 uses **native USB**
(USB-Serial/JTAG). From that follows: the port re-enumerates on every hard reset, so the path
a board answered on before a flash is not the path it answers on after; `ampy` hangs on this
REPL, so every file operation goes through `mpremote`; `esptool`'s auto-reset does not work, so
download mode is entered over the wire from MicroPython itself and the buttons are the
fallback; and only one program may hold a port at a time, so a capture left running makes every
later step fail with a busy device.

That knowledge currently lives in one skill file and in three sessions of tribal memory, which
is a large part of what makes first contact with these boards painful for anyone else. It
belongs in code that runs.

Every external command goes through a `Runner`, injected, so the whole layer is testable
without a board on the desk.
"""
import glob
import shutil
import subprocess
import time as _time

# The one file operation tool for these boards. `ampy` hangs on the USB-Serial/JTAG REPL.
MPREMOTE = "mpremote"

# `esptool` has shipped under both names across its versions, and a machine may have either.
# Resolved rather than insisted on, so a working install is not rejected over its filename.
ESPTOOL_NAMES = ("esptool.py", "esptool")
ESPTOOL = ESPTOOL_NAMES[0]


def resolve_esptool(which=None):
    """Whichever name esptool answers to here, or the canonical one to complain about."""
    which = which or shutil.which
    for name in ESPTOOL_NAMES:
        if which(name):
            return name
    return ESPTOOL

CHIP = "esp32s3"

# Where a T3S3 shows up. macOS names native-USB CDC devices `cu.usbmodem*`; the Linux names are
# here so the wizard is not a mac-only tool, and other USB serial devices (a phone, another dev
# board) can match these too, which is exactly why a candidate is probed and never assumed.
PORT_GLOBS = ("/dev/cu.usbmodem*", "/dev/ttyACM*", "/dev/ttyUSB*")

_PROBE_TOKEN = "ALLORA_PROBE"

# How long a probe waits for a REPL that may not be there. Generous, because it is the
# ceiling on a healthy board answering slowly, not a budget for a dead one.
PROBE_TIMEOUT = 15

# The same value the node itself reports: `Node.MAC` is the last 8 hex characters of the WLAN
# interface's address, and on these boards that is what tells two identical T3S3s apart before
# either has an identity. Best-effort, because a board whose connector takes its address from
# somewhere else (a LoPy4, an E5) has no WLAN to ask.
_MAC_SNIPPET = (
    "try:\n"
    "    import ubinascii, network\n"
    "    w = network.WLAN(network.STA_IF); w.active(True)\n"
    "    print('MAC:' + ubinascii.hexlify(w.config('mac')).decode()[-8:])\n"
    "    w.active(False)\n"
    "except Exception as e:\n"
    "    print('MAC:unknown')\n"
)

# Asks the board for its identity through the library's own code, creating the key on the spot
# if the board has none. Deriving it this way rather than reading a boot banner off the serial
# line avoids the race the native-USB port makes unavoidable: the banner is printed while the
# port is still re-enumerating, so it is routinely missed. It is also the same call the node
# itself makes, so the fingerprint is the one the node will use, not a second opinion about it.
_IDENTITY_SNIPPET = (
    "from AlLoRa.Security.identity import load_or_create_identity\n"
    "from os import urandom\n"
    "print('DEVID:' + load_or_create_identity({path!r}, urandom)[2].hex())\n"
)


class BoardError(RuntimeError):
    """Something the board or a tool said no to, phrased for whoever is at the bench."""


class Runner:
    """Runs an external command. The one place this package touches the outside world."""

    def run(self, argv, timeout=120, capture=True):
        try:
            completed = subprocess.run(
                argv, timeout=timeout,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None)
        except FileNotFoundError:
            raise BoardError(
                "{} is not on PATH. The wizard shells out to `mpremote` and `esptool`, which "
                "are deliberately not part of the AlLoRa install. Run `provision doctor` and "
                "it will tell you exactly how to get them, or `provision doctor --install` and "
                "it will do it.".format(argv[0]))
        except subprocess.TimeoutExpired:
            raise BoardError(
                "{} did not finish within {}s. On these boards that usually means something "
                "else is holding the port: close any capture, `mpremote` or `screen` on "
                "it.".format(argv[0], timeout))
        out = (completed.stdout or b"").decode("utf-8", "replace")
        err = (completed.stderr or b"").decode("utf-8", "replace")
        return completed.returncode, out, err


def candidate_ports(globs=PORT_GLOBS):
    """Every serial device that could be a board. Not every one of them is.

    Other USB serial devices can be present and they are not T3S3s, so this is the start of
    discovery and never the end of it: a candidate becomes a board only once it answers.
    """
    found = []
    for pattern in globs:
        found.extend(glob.glob(pattern))
    return sorted(set(found))


def discover_boards(runner=None, globs=PORT_GLOBS):
    """The candidate ports that answer a MicroPython REPL, in discovery order."""
    runner = runner or Runner()
    return [port for port in candidate_ports(globs) if Board(port, runner=runner).alive()]


class Board:
    """One board, addressed by the port it is currently answering on."""

    def __init__(self, port, runner=None, mpremote=MPREMOTE, esptool=None, sleep=None,
                 which=None):
        self.port = port
        self.runner = runner or Runner()
        self.mpremote = mpremote
        self.esptool = esptool or resolve_esptool(which)
        self._sleep = sleep or _time.sleep

    # --- the REPL -----------------------------------------------------------------------

    def _mpremote(self, args, timeout=60):
        return self.runner.run([self.mpremote, "connect", self.port] + list(args),
                               timeout=timeout)

    def alive(self, timeout=PROBE_TIMEOUT):
        """Whether a MicroPython REPL answers on this port right now.

        The timeout is the cost of a *negative* answer and nothing else: a board that is there
        replies in a second or two, while a port that enumerates and answers nothing blocks
        `mpremote` for the whole of it. That is why the post-flash wait passes a shorter one --
        it asks this question repeatedly of a port that is silent by definition.
        """
        try:
            code, out, _ = self._mpremote(["exec", "print('{}')".format(_PROBE_TOKEN)],
                                          timeout=timeout)
        except BoardError:
            return False
        return code == 0 and _PROBE_TOKEN in out

    def exec(self, code, timeout=60):
        """Run a snippet on the board and return its stdout.

        This interrupts whatever the board was running, which is deliberate and is how a
        provisioned node is questioned: `main.py` is a loop that never returns, so waiting for
        it politely would wait forever.
        """
        returncode, out, err = self._mpremote(["exec", code], timeout=timeout)
        if returncode != 0:
            raise BoardError("the board at {} refused a command: {}".format(
                self.port, (err or out).strip()))
        return out

    def mac(self):
        """The board's short MAC, or None if it could not be asked.

        Discovery answers "is something there"; this answers "which board is it". Two T3S3s on a
        desk are indistinguishable until one of them says a number, and the operator needs that
        before deciding which one is the Edge.
        """
        try:
            out = self.exec(_MAC_SNIPPET, timeout=30)
        except BoardError:
            return None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("MAC:"):
                value = line[len("MAC:"):].strip()
                return None if value == "unknown" else value
        return None

    def wait_for_repl(self, attempts=10, delay=1.0, probe_timeout=PROBE_TIMEOUT,
                      on_attempt=None):
        """Poll until the REPL answers, or give up.

        Needed after a flash: the write ends in a hard reset, the native-USB port
        re-enumerates, and `mpremote` reports `could not enter raw repl` until the board has
        come back. On a board that stays silent the caller escalates: a reset over the wire
        first, and only then a finger on RESET.

        `on_attempt(number, attempts)` is called before each probe. A caller that has a person
        watching passes one, because the cost of this wait is measured in minutes and a
        terminal that prints nothing for two of them is indistinguishable from a hang.
        """
        for attempt in range(attempts):
            if on_attempt is not None:
                on_attempt(attempt + 1, attempts)
            if self.alive(timeout=probe_timeout):
                return True
            if attempt < attempts - 1:
                self._sleep(delay)
        return False

    def wake(self, timeout=60, settle=5):
        """Hard-reset the board over the wire, for when the flash's own reset did not take.

        `flash()` already ends its write with `--after hard_reset`, and on this hardware the
        board still comes back silent: enumerated, answering no REPL, sitting below
        MicroPython. A second, separate `esptool` call issues a reset the board does act on,
        which is what makes the tap on RESET avoidable rather than routine. Measured on the
        bench on 2026-08-27: the board never returned by itself, and this recovered it every
        time it was tried.

        The settle matters as much as the call. The reset lands, the native-USB port
        re-enumerates, and a probe fired immediately reads as a failure on a board that is
        about to be fine.

        Only ever called against a port that answers nothing: this puts a board into the ROM
        loader, so aiming it at a healthy board would take it down rather than bring it back.
        """
        returncode, _, _ = self.runner.run(
            [self.esptool, "--port", self.port, "--after", "hard_reset", "chip_id"],
            timeout=timeout)
        self._sleep(settle)
        return returncode == 0

    # --- files --------------------------------------------------------------------------

    # What `mpremote fs cat` says when the file genuinely is not there, as opposed to when the
    # read failed for some other reason. The difference matters more than it looks: a read that
    # failed because the port was busy, and was reported as "no such file", makes a board that
    # holds an identity look like a fresh one, and the next step erases it.
    _ABSENT_MARKERS = ("enoent", "no such file", "errno 2")

    def read_remote(self, name, timeout=60):
        """The contents of a file on the board, or None if the board has no such file.

        Raises rather than returning None when the read failed for any other reason. A caller
        cannot act on "None" if None means both "there is nothing here" and "I could not
        look".
        """
        returncode, out, err = self._mpremote(["fs", "cat", name], timeout=timeout)
        if returncode == 0:
            return out
        message = (err or out).strip()
        if any(marker in message.lower() for marker in self._ABSENT_MARKERS):
            return None
        raise BoardError(
            "could not read {} from the board at {}: {}".format(name, self.port, message))

    def write_remote(self, local_path, name, timeout=120):
        returncode, out, err = self._mpremote(
            ["fs", "cp", local_path, ":" + name], timeout=timeout)
        if returncode != 0:
            raise BoardError("could not write {} to the board at {}: {}".format(
                name, self.port, (err or out).strip()))
        return name

    def mkdir_remote(self, name, timeout=60):
        """Make a directory on the board, treating "it is already there" as success.

        `mpremote fs mkdir` fails on an existing directory, and re-provisioning a board that
        already has its outbound folder is the ordinary case rather than an error. Nothing else
        distinguishes the two outcomes, so the caller is told it worked either way and finds out
        for real when the file it wanted to put there is written.
        """
        self._mpremote(["fs", "mkdir", name], timeout=timeout)
        return name

    def remove_remote(self, name, timeout=60):
        returncode, _, _ = self._mpremote(["fs", "rm", name], timeout=timeout)
        return returncode == 0

    # --- identity -----------------------------------------------------------------------

    def identity_material(self, path="identity.key"):
        """The board's stored identity scalar, or None if it has never made one.

        Raises if the board could not be asked, which is the whole point of asking: the caller
        is about to erase this file.
        """
        raw = self.read_remote(path)
        if raw is None:
            return None
        material = raw.strip()
        return material or None

    def ensure_identity(self, path="identity.key", timeout=120):
        """The board's `device_id`, as hex, generating and persisting the key if it has none.

        The key is made *on the board* and never on the laptop. That is what makes a
        `device_id` something the board proves it holds rather than something an operator
        assigned it, and a wizard that minted these on the host would invent a flow the
        library does not have.
        """
        out = self.exec(_IDENTITY_SNIPPET.format(path=path), timeout=timeout)
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("DEVID:"):
                return line[len("DEVID:"):].strip()
        raise BoardError(
            "the board at {} did not report a device_id. That usually means the firmware "
            "predates the secure path: AlLoRa is frozen into the image, so a library change "
            "only reaches the board through a fresh build.".format(self.port))

    # --- flashing -----------------------------------------------------------------------

    def enter_bootloader(self, timeout=30):
        """Put the board in download mode over the wire, so a flash needs nobody at the bench.

        `esptool`'s auto-reset does not work on native USB, so the alternative is holding BOOT
        and tapping RESET by hand. Asking MicroPython to do it works whenever the board is
        still running MicroPython, which is every case except a board that is bricked or was
        never flashed.
        """
        returncode, out, err = self._mpremote(
            ["exec", "import machine; machine.bootloader()"], timeout=timeout)
        # The board vanishes mid-command by design: it reboots into the ROM loader and the
        # port re-enumerates under it, so mpremote frequently reports failure on a call that
        # did exactly what it was asked. The result is decided by whether the loader answers,
        # not by this return code.
        self._sleep(2)
        return returncode == 0

    def flash(self, bin_path, timeout=600):
        """Erase and write the firmware, keeping the board in the loader in between.

        The two commands are chained rather than run independently because a hard reset out of
        download mode would need another BOOT hold: the erase is told not to reset afterwards,
        and the write is told the board is already in the loader and to hard-reset out of it
        at the end.
        """
        erase = [self.esptool, "--chip", CHIP, "--port", self.port,
                 "--after", "no_reset", "erase_flash"]
        write = [self.esptool, "--chip", CHIP, "--port", self.port,
                 "--before", "no_reset", "--after", "hard_reset",
                 "write_flash", "-z", "0x0", bin_path]
        for argv in (erase, write):
            returncode, out, err = self.runner.run(argv, timeout=timeout)
            if returncode != 0:
                raise BoardError(
                    "{} failed on {}: {}\nIf the board is not in download mode, hold BOOT, tap "
                    "RESET, release BOOT, and run this again.".format(
                        argv[-1] if argv is erase else "write_flash", self.port,
                        (err or out).strip()))
        return True

    # --- running ------------------------------------------------------------------------

    def soft_reset(self, timeout=30):
        """Restart the board into the program that was just pushed.

        A soft reset rather than a hard one, so the native-USB port does not re-enumerate and
        the board keeps the name the wizard has been addressing it by. Best-effort by nature:
        the board goes away in the middle of the command, so the return code says little, and
        what matters is that the REPL comes back.
        """
        self._mpremote(["exec", "import machine; machine.soft_reset()"], timeout=timeout)
        self._sleep(2)
        return self.wait_for_repl(attempts=5, delay=1.0)

    def run_script(self, local_path, timeout=300):
        """Run a local script on the board and return everything it printed.

        `mpremote run` soft-resets, which does *not* re-enumerate the port, so its output is
        caught from the script's first `print`. A capture attached to the serial device cannot
        do that: the port drops on a hard reset and comes back under a new name, and the
        capture is left holding a device that no longer exists.
        """
        returncode, out, err = self.runner.run(
            [self.mpremote, "connect", self.port, "run", local_path], timeout=timeout)
        return returncode, out, err

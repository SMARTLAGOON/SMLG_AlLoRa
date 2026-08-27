"""Whether this machine can drive a board, and what to do when it cannot.

The wizard needs two executables it deliberately does not install as dependencies: `mpremote`
for every file operation, and `esptool` for the flash. Saying "install them" and stopping is
the least useful moment to be unhelpful, and on a machine with several Pythons it is also not
enough information to act on. Two traps live here, and both produce a tool that looks installed
and does not work:

**The executable is not the import.** `pip install mpremote` puts a script in one interpreter's
scripts directory. If that directory is not on PATH, the install succeeds and the wizard still
cannot find it, which reads as the install having failed.

**More than one copy can be installed.** Where a machine has several Pythons, the one that
answers to `python3` is often not the one whose scripts directory holds the tools, and a pip
install from the wrong interpreter leaves two copies on PATH. The earlier one wins and nothing
anywhere says which ran, so the wizard can be driving a different build than the person testing
the same command by hand. Every copy found is reported, in the order PATH resolves them.

So this module works out where a script would land and whether that place is on PATH *before*
anything is installed, and looks again *after*, because pip reporting success says the package
is on the machine and not that the wizard can run it.
"""
import os
import shutil
import subprocess
import sys
import sysconfig

from tools.allora_provision.board import Runner


class Requirement:
    """One external executable, and how to ask it for its version and install it."""

    def __init__(self, key, executables, package, why, version_args):
        self.key = key
        # In preference order. `esptool` grew a second name across versions, and a machine may
        # have either, so the wizard resolves whichever is there rather than insisting on one.
        self.executables = tuple(executables)
        self.package = package
        self.why = why
        self.version_args = list(version_args)


REQUIREMENTS = (
    Requirement("mpremote", ("mpremote",), "mpremote",
                "every file operation and every REPL question. `ampy` hangs on the "
                "USB-Serial/JTAG REPL these boards use, so it is not an alternative.",
                ["--version"]),
    Requirement("esptool", ("esptool.py", "esptool"), "esptool",
                "erasing and writing firmware. Only needed when you pass --firmware.",
                ["version"]),
)


def resolve(requirement, which=None):
    """The path to this requirement's executable, or None. Also returns which name matched."""
    which = which or shutil.which
    for name in requirement.executables:
        path = which(name)
        if path:
            return name, path
    return None, None


def copies_on_path(requirement, path_env=None):
    """Every copy of this requirement's executable that PATH can reach, in the order it wins.

    A machine that has been pip-installed from two interpreters ends up with two, and the one
    that runs is decided by PATH order alone. Whichever question somebody is asking when a
    board misbehaves, "am I even running the build I think I am" should not be one of the ones
    they have to work out for themselves.
    """
    entries = (path_env if path_env is not None
               else os.environ.get("PATH", "")).split(os.pathsep)
    found = []
    for entry in entries:
        if not entry:
            continue
        for name in requirement.executables:
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                if candidate not in found:
                    found.append(candidate)
    return found


def _version(path, requirement, runner):
    try:
        code, out, err = runner.run([path] + requirement.version_args, timeout=30)
    except Exception:
        return None
    if code != 0:
        # A tool that will not say its version is still a usable tool. Reporting whatever it
        # printed while failing would put an error string where a version belongs.
        return None
    text = (out or err or "").strip().splitlines()
    return text[0].strip() if text else None


def path_rank(directory):
    """Where a directory sits in PATH, or None if it is not on it.

    Order is the whole question when two copies of a tool exist: the earlier one wins, and
    nothing anywhere says which one ran.
    """
    if not directory:
        return None
    target = os.path.normpath(directory)
    for index, entry in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if entry and os.path.normpath(entry) == target:
            return index
    return None


def install_plan(requirement, python=None, runner=None, which=None):
    """Where installing this requirement would put its executable, and what that would mean.

    Reported before anything runs, because both things that can go wrong here are invisible
    afterwards: a scripts directory that is not on PATH looks like a failed install, and a
    scripts directory that is *early* on PATH silently replaces a working tool.
    """
    runner = runner or Runner()
    python = python or sys.executable
    scripts, user_scripts = _scripts_dirs(python, runner)

    # Plain install when we can write where the scripts go, which covers a virtualenv, a conda
    # environment, and a user-owned prefix. Otherwise --user, which is the one that does not
    # need root and the one whose directory is most often missing from PATH.
    if scripts and os.access(scripts, os.W_OK):
        extra, target = [], scripts
    else:
        extra, target = ["--user"], user_scripts

    argv = [python, "-m", "pip", "install"] + extra + [requirement.package]
    return {"python": python, "argv": argv, "command": " ".join(argv),
            "scripts_dir": target, "on_path": path_rank(target) is not None,
            "package": requirement.package}


_SCRIPTS_PROBE = ("import os, sysconfig\n"
                  "print(sysconfig.get_path('scripts'))\n"
                  "print(sysconfig.get_path('scripts', os.name + '_user'))\n")


def _scripts_dirs(python, runner):
    """Where this interpreter puts installed scripts, both plain and `--user`.

    Answered in-process for the interpreter running the wizard, which is the ordinary case: it
    is the same question `sysconfig` answers here, and asking it through a subprocess would
    make a fact about this host depend on the runner that talks to boards. Only a `--python`
    naming somebody else's interpreter has to be shelled out to.
    """
    if python is None or os.path.normpath(python) == os.path.normpath(sys.executable):
        return (sysconfig.get_path("scripts"),
                sysconfig.get_path("scripts", os.name + "_user"))
    try:
        code, out, _ = runner.run([python, "-c", _SCRIPTS_PROBE], timeout=30)
        if code != 0:
            return None, None
        lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
        return (lines[0] if lines else None, lines[1] if len(lines) > 1 else None)
    except Exception:
        return None, None


def check(requirements=REQUIREMENTS, runner=None, which=None, python=None):
    """One report per requirement: what is there, and what installing would do if it is not."""
    runner = runner or Runner()
    report = []
    for requirement in requirements:
        name, path = resolve(requirement, which=which)
        entry = {"tool": requirement.key, "package": requirement.package,
                 "why": requirement.why, "found": path is not None,
                 "executable": name, "path": path, "version": None, "plan": None,
                 "copies": copies_on_path(requirement)}
        if path:
            entry["version"] = _version(path, requirement, runner)
        else:
            entry["plan"] = install_plan(requirement, python=python, runner=runner, which=which)
        report.append(entry)
    return report


def missing(report):
    return [entry for entry in report if not entry["found"]]


def install(requirement, python=None, runner=None, which=None):
    """Install one requirement, then look for its executable again.

    The second look is the point. `pip install` reporting success says the package is on the
    machine; it does not say the wizard can run it, and those two come apart exactly when the
    scripts directory is not on PATH.
    """
    runner = runner or Runner()
    plan = install_plan(requirement, python=python, runner=runner, which=which)
    code, out, err = runner.run(plan["argv"], timeout=600)
    if code != 0:
        return {"ok": False, "plan": plan, "detail": (err or out).strip()}

    # shutil.which caches nothing, but a freshly created script in a directory already on PATH
    # is found only if we look again rather than trusting the earlier answer.
    name, path = resolve(requirement, which=which)
    if path:
        return {"ok": True, "plan": plan, "path": path,
                "detail": "installed and reachable as {}".format(name)}
    return {"ok": False, "plan": plan, "path": None,
            "detail": ("{} installed, but no executable is on PATH. It went to {}, which is not "
                       "a directory PATH lists. Add it: export "
                       "PATH=\"{}:$PATH\"".format(plan["package"], plan["scripts_dir"],
                                                  plan["scripts_dir"]))}

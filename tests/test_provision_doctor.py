"""Whether this machine can drive a board, and what the wizard says when it cannot.

Two failures live here and both end with a tool that looks installed and does not work. The
first is that the executable is not the import: `pip install mpremote` puts a script in one
interpreter's scripts directory, and if that directory is not on PATH the install succeeds and
the wizard still cannot find it. The second is that more than one copy can end up installed,
in which case PATH order alone decides which build runs and nothing says so.

Both are checked before anything is installed, and the install looks again afterwards rather
than trusting pip's exit code, because pip succeeding says the package is on the machine and
not that the wizard can run it.
"""
import os

import pytest

from tools.allora_provision import doctor
from tools.allora_provision.board import ESPTOOL_NAMES, resolve_esptool

MPREMOTE = doctor.REQUIREMENTS[0]
ESPTOOL = doctor.REQUIREMENTS[1]


class FakeRunner:
    def __init__(self, replies=None, default=(0, "", "")):
        self.calls = []
        self.replies = replies or []
        self.default = default

    def run(self, argv, timeout=120, capture=True):
        self.calls.append(list(argv))
        for match, reply in self.replies:
            if match(argv):
                return reply
        return self.default


def _which(table):
    return lambda name: table.get(name)


def _scripts(scripts, user_scripts):
    return [(lambda argv: "-c" in argv, (0, scripts + "\n" + user_scripts + "\n", ""))]


# --- resolving ---------------------------------------------------------------------------

def test_esptool_is_accepted_under_either_of_its_names():
    """It has shipped as both across versions. Rejecting a working install over its filename
    would be the wizard inventing a problem."""
    name, path = doctor.resolve(ESPTOOL, which=_which({"esptool": "/usr/local/bin/esptool"}))
    assert (name, path) == ("esptool", "/usr/local/bin/esptool")
    name, path = doctor.resolve(ESPTOOL, which=_which({"esptool.py": "/opt/bin/esptool.py"}))
    assert (name, path) == ("esptool.py", "/opt/bin/esptool.py")
    assert doctor.resolve(ESPTOOL, which=_which({})) == (None, None)


def test_the_board_layer_calls_whichever_name_is_actually_there():
    assert resolve_esptool(which=_which({"esptool": "/usr/local/bin/esptool"})) == "esptool"
    assert resolve_esptool(which=_which({})) == ESPTOOL_NAMES[0]


def test_path_rank_is_where_a_directory_sits_in_path(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/first", "/second", "/third"]))
    assert doctor.path_rank("/first") == 0
    assert doctor.path_rank("/third") == 2
    assert doctor.path_rank("/nowhere") is None
    assert doctor.path_rank(None) is None


def test_every_copy_on_path_is_reported_in_the_order_that_decides_which_runs(tmp_path):
    """PATH order alone decides which build runs, and nothing anywhere says which one did."""
    first, second = tmp_path / "a", tmp_path / "b"
    for directory in (first, second):
        directory.mkdir()
        script = directory / "esptool.py"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
    copies = doctor.copies_on_path(
        ESPTOOL, path_env=os.pathsep.join([str(first), str(second)]))
    assert copies == [str(first / "esptool.py"), str(second / "esptool.py")]

    reversed_copies = doctor.copies_on_path(
        ESPTOOL, path_env=os.pathsep.join([str(second), str(first)]))
    assert reversed_copies[0] == str(second / "esptool.py")


def test_a_directory_listed_twice_on_path_is_not_two_copies(tmp_path):
    script = tmp_path / "mpremote"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    duplicated = os.pathsep.join([str(tmp_path), str(tmp_path)])
    assert doctor.copies_on_path(MPREMOTE, path_env=duplicated) == [str(script)]


# --- planning an install -----------------------------------------------------------------

def test_a_writable_scripts_directory_gets_a_plain_install(tmp_path, monkeypatch):
    scripts = tmp_path / "bin"
    scripts.mkdir()
    monkeypatch.setenv("PATH", str(scripts))
    plan = doctor.install_plan(
        MPREMOTE, python="/opt/py/bin/python",
        runner=FakeRunner(replies=_scripts(str(scripts), str(tmp_path / "user"))),
        which=_which({}))
    assert plan["argv"] == ["/opt/py/bin/python", "-m", "pip", "install", "mpremote"]
    assert plan["scripts_dir"] == str(scripts)
    assert plan["on_path"] is True


def test_an_unwritable_scripts_directory_falls_back_to_a_user_install(tmp_path, monkeypatch):
    """The one that does not need root, and the one whose directory is most often missing from
    PATH, which is why the plan reports both facts together."""
    user_scripts = tmp_path / "userbin"
    user_scripts.mkdir()
    monkeypatch.setenv("PATH", "/somewhere/else")
    plan = doctor.install_plan(
        MPREMOTE, python="/usr/bin/python3",
        runner=FakeRunner(replies=_scripts("/a/place/that/does/not/exist", str(user_scripts))),
        which=_which({}))
    assert "--user" in plan["argv"]
    assert plan["scripts_dir"] == str(user_scripts)
    assert plan["on_path"] is False


# --- doing the install -------------------------------------------------------------------

def test_a_pip_that_failed_is_reported_with_what_pip_said(tmp_path):
    runner = FakeRunner(replies=_scripts(str(tmp_path), str(tmp_path)),
                        default=(1, "", "No matching distribution"))
    outcome = doctor.install(MPREMOTE, python="/opt/py/bin/python", runner=runner,
                             which=_which({}))
    assert outcome["ok"] is False
    assert "No matching distribution" in outcome["detail"]


def test_a_pip_that_succeeded_into_a_directory_off_path_is_not_success(tmp_path):
    """This is the failure that reads as the install not having worked. pip's exit code says
    the package is on the machine; it does not say the wizard can run it."""
    runner = FakeRunner(replies=_scripts("/nope", str(tmp_path / "userbin")))
    outcome = doctor.install(MPREMOTE, python="/usr/bin/python3", runner=runner,
                             which=_which({}))
    assert outcome["ok"] is False
    assert "no executable is on PATH" in outcome["detail"]
    assert "export PATH=" in outcome["detail"]


def test_an_install_that_lands_somewhere_reachable_is_reported_as_reachable(tmp_path):
    runner = FakeRunner(replies=_scripts(str(tmp_path), str(tmp_path)))
    outcome = doctor.install(MPREMOTE, python="/opt/py/bin/python", runner=runner,
                             which=_which({"mpremote": str(tmp_path / "mpremote")}))
    assert outcome["ok"] is True
    assert outcome["path"] == str(tmp_path / "mpremote")


# --- the report --------------------------------------------------------------------------

def test_a_found_tool_reports_its_version_and_a_missing_one_reports_a_plan(tmp_path):
    runner = FakeRunner(
        replies=[(lambda argv: argv[0].endswith("mpremote"), (0, "mpremote 1.28.0\n", "")),
                 (lambda argv: "-c" in argv, (0, str(tmp_path) + "\n" + str(tmp_path) + "\n", ""))])
    report = doctor.check(runner=runner, which=_which({"mpremote": "/opt/bin/mpremote"}))
    by_tool = {entry["tool"]: entry for entry in report}

    assert by_tool["mpremote"]["found"] is True
    assert by_tool["mpremote"]["version"] == "mpremote 1.28.0"
    assert by_tool["mpremote"]["plan"] is None

    assert by_tool["esptool"]["found"] is False
    assert by_tool["esptool"]["plan"]["package"] == "esptool"
    assert doctor.missing(report) == [by_tool["esptool"]]


def test_a_tool_that_cannot_say_its_version_is_still_a_found_tool():
    """Version reporting is a nicety. Refusing to proceed over it would be the wizard failing
    on the one thing it does not need."""
    runner = FakeRunner(default=(1, "", "unrecognised option"))
    report = doctor.check(requirements=[MPREMOTE], runner=runner,
                          which=_which({"mpremote": "/opt/bin/mpremote"}))
    assert report[0]["found"] is True
    assert report[0]["version"] is None
    assert doctor.missing(report) == []

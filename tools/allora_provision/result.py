"""One structured result per command, so a caller has one thing to parse.

With `--json`, stdout carries exactly one JSON document and progress goes to stderr, so the
control website can parse stdout as it is, and a person still sees the steps. Without `--json`,
both go to stdout in order.
"""
import json
import sys

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


class Result:
    """What one command did, in the shape a person and a backend both read."""

    def __init__(self, command, json_mode=False, stream=None):
        self.command = command
        self.json_mode = json_mode
        self.steps = []
        self.warnings = []
        self.data = {}
        self.error = None
        # Progress goes here. Under --json that is stderr, which keeps stdout a single
        # parseable document; otherwise it is stdout, where a person is already looking.
        self._stream = stream if stream is not None else (sys.stderr if json_mode else sys.stdout)

    def step(self, name, detail="", status=OK):
        self.steps.append({"step": name, "status": status, "detail": detail})
        marker = {OK: "  ok", FAILED: "FAIL", SKIPPED: "skip"}.get(status, status)
        self._say("[{}] {}{}".format(marker, name, ": " + detail if detail else ""))
        return self

    def warn(self, message):
        self.warnings.append(message)
        self._say("[warn] " + message)
        return self

    def note(self, message):
        """Something worth a person seeing that is not a step and not a warning."""
        self._say(message)
        return self

    def set(self, **values):
        self.data.update(values)
        return self

    def fail(self, message):
        self.error = message
        self.step("failed", message, status=FAILED)
        return self

    @property
    def ok(self):
        return self.error is None

    def to_dict(self):
        return {"command": self.command, "ok": self.ok, "error": self.error,
                "steps": self.steps, "warnings": self.warnings, "data": self.data}

    def emit(self, out=None):
        """Write the final result and return the process exit code."""
        out = out if out is not None else sys.stdout
        if self.json_mode:
            out.write(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n")
        elif self.ok:
            out.write("\n{} completed.\n".format(self.command))
        else:
            out.write("\n{} failed: {}\n".format(self.command, self.error))
        out.flush()
        return 0 if self.ok else 1

    def _say(self, line):
        if self._stream is not None:
            self._stream.write(line + "\n")
            self._stream.flush()

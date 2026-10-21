"""Make AES unavailable for one test, the way a real install fails.

No board AES module (`cryptolib`, `ucryptolib`), and the cryptography package failing to import
with the error a real install raises, made fresh for each import: `absent` when it is not
installed, or any other factory for one that is installed but broken.
"""


def absent():
    return ModuleNotFoundError("No module named 'cryptography'", name="cryptography")


def as_micropython(monkeypatch):
    """Report the interpreter as MicroPython, changing nothing else about sys.implementation."""
    import sys
    import types
    fields = dict(vars(sys.implementation), name="micropython")
    monkeypatch.setattr(sys, "implementation", types.SimpleNamespace(**fields))


def block(monkeypatch, make_error):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name in ("cryptolib", "ucryptolib"):
            raise ImportError("no module named {}".format(name))
        if name == "cryptography" or name.startswith("cryptography."):
            raise make_error()
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)

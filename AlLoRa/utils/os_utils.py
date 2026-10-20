try:
    # For regular Python
    import os
except ImportError:
    # For MicroPython
    import uos as os

_EEXIST = 17    # the same number on CPython and MicroPython, so no errno import is needed


def make_dir(path):
    """Create `path`; a folder already there is success. Any other failure is returned."""
    try:
        os.mkdir(path)
    except Exception as e:
        if getattr(e, "args", None) and e.args[0] == _EEXIST:
            return None
        return e
    return None

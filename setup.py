from setuptools import setup, find_packages
import codecs
import os
import re

here = os.path.abspath(os.path.dirname(__file__))
REPO_URL = "https://github.com/SMARTLAGOON/SMLG_AlLoRa"

# The version is written once, in AlLoRa/__init__.py, where a board can read it too.
with codecs.open(os.path.join(here, "AlLoRa", "__init__.py"), encoding="utf-8") as fh:
    found = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', fh.read(), re.M)
if not found:
    raise SystemExit("AlLoRa/__init__.py has no __version__ = \"x.y.z\" line")
VERSION = found.group(1)


def _absolute(target, image):
    # PyPI shows the README as the package page, where a path relative to the repo leads
    # nowhere, so each one is pinned to this release's tag on GitHub.
    if target.startswith(("http://", "https://", "#", "mailto:")):
        return target
    if image:
        return "https://raw.githubusercontent.com/SMARTLAGOON/SMLG_AlLoRa/v%s/%s" % (VERSION, target)
    return "%s/blob/v%s/%s" % (REPO_URL, VERSION, target)


with codecs.open(os.path.join(here, "README.md"), encoding="utf-8") as fh:
    long_description = "\n" + fh.read()
long_description = re.sub(
    r'(<img[^>]*\ssrc=")([^"]+)(")',
    lambda m: m.group(1) + _absolute(m.group(2), True) + m.group(3), long_description)
long_description = re.sub(
    r"(!?)(\[[^\]]*\]\()([^)\s]+)(\))",
    lambda m: m.group(1) + m.group(2) + _absolute(m.group(3), m.group(1) == "!") + m.group(4),
    long_description)
DESCRIPTION = 'Modular, mesh and multi-device Advanced LoRa Content Transfer Protocol for IoT applications'

# Setting up
setup(
    name="AlLoRa",
    version=VERSION,
    author="Benjamín Arratia",
    author_email="<hello@barratia.com>",
    description=DESCRIPTION,
    long_description_content_type="text/markdown",
    long_description=long_description,
    url=REPO_URL,
    project_urls={"Source": REPO_URL, "Releases": REPO_URL + "/releases"},
    # Only the library: examples/ are programs to copy onto a board, not modules to import.
    packages=find_packages(include=["AlLoRa", "AlLoRa.*"]),
    # AES on a computer or a Pi, for secure mode, the posture the tools default to (a board uses
    # its own AES).
    install_requires=["cryptography"],
    keywords=['Python', 'Micropython', 'LoRa', 'mesh', 'IoT', 'Environmental Intelligence'],
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: Implementation :: MicroPython",
        "Operating System :: Unix",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows"
    ]
)
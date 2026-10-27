"""The README is also the package page on PyPI, where a path relative to the repo leads nowhere.

setup.py rewrites every relative link and image in the README into a full GitHub address pinned
to the release tag, so the page on pypi.org shows the figures and its links reach the repo.
In-page anchors are left as they are; PyPI gives headings no anchors, so they do nothing there.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "https://github.com/SMARTLAGOON/SMLG_AlLoRa"
RAW = "https://raw.githubusercontent.com/SMARTLAGOON/SMLG_AlLoRa"

MD_TARGET = re.compile(r"\]\(([^)\s]+)\)")
HTML_SRC = re.compile(r'<img[^>]*\ssrc="([^"]+)"')


def _description():
    out = subprocess.run(
        [sys.executable, "setup.py", "--long-description"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout


def _version():
    with open(os.path.join(ROOT, "AlLoRa", "__init__.py"), encoding="utf-8") as fh:
        return re.search(r'^__version__\s*=\s*["\']([^"\']+)', fh.read(), re.M).group(1)


def _is_relative(target):
    return not target.startswith(("http://", "https://", "#", "mailto:"))


def test_no_relative_link_or_image_is_left():
    text = _description()
    left = [t for t in MD_TARGET.findall(text) + HTML_SRC.findall(text) if _is_relative(t)]
    assert left == []


def test_images_point_at_raw_files_of_the_release_tag():
    text = _description()
    tag = "v" + _version()
    assert '<img src="%s/%s/readme_assets/logo.png"' % (RAW, tag) in text
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", text)
    assert images
    assert all(i.startswith("%s/%s/" % (RAW, tag)) for i in images if "SMLG_AlLoRa" in i)


def test_links_point_at_the_release_tag_and_keep_their_anchor():
    text = _description()
    tag = "v" + _version()
    assert "%s/blob/%s/examples/Readme.md#your-first-transfer" % (REPO, tag) in text


def test_in_page_anchors_and_full_addresses_are_untouched():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    text = _description()
    kept = [t for t in MD_TARGET.findall(readme) if not _is_relative(t)]
    assert kept
    for t in kept:
        assert "](%s)" % t in text

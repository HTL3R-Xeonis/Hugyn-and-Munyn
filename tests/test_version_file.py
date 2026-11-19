"""The release version: the one-line VERSION file at the repository root is valid SemVer.

The release workflow compares the pushed tag vX.Y.Z with it, so a version that is not SemVer would block a release.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# SemVer 2.0 without build metadata: X.Y.Z, or X.Y.Z-rc.N for a release candidate (the release tag must equal it).
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-(0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
)


def test_the_version_file_is_one_semver_line():
    text = (ROOT / "VERSION").read_text()
    assert text.endswith("\n") and text.count("\n") == 1
    assert SEMVER.fullmatch(text.strip())


def test_the_semver_pattern_takes_release_candidates_and_refuses_the_rest():
    assert SEMVER.fullmatch("1.3.0-rc.1")
    for bad in ("1.3", "v1.3.0", "01.3.0", "1.3.0-", "1.3.0-rc..1", "1.3.0 "):
        assert not SEMVER.fullmatch(bad), bad

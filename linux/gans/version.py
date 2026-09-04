"""The app version, resolved from (in order):

1. A ``VERSION`` file next to this module — stamped by ``packaging/build-deb.sh`` from the
   release tag, so the installed app reports the released version.
2. ``MARKETING_VERSION`` in the Xcode project when running from the source tree, so the
   Linux and macOS builds can never drift apart.
3. ``0.0.0-dev``.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEV_VERSION = "0.0.0-dev"


def app_version() -> str:
    stamped = _HERE / "VERSION"
    try:
        text = stamped.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass

    pbxproj = _HERE.parent.parent / "Gans.xcodeproj" / "project.pbxproj"
    try:
        match = re.search(r"MARKETING_VERSION\s*=\s*([0-9][0-9A-Za-z.\-]*)\s*;", pbxproj.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    except OSError:
        pass
    return _DEV_VERSION

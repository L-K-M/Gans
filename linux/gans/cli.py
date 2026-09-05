"""Command-line entry point.

    gans                 start the tray agent (or, if it's already running, open Quick Search)
    gans toggle          show/hide Quick Search in the running instance
    gans search          show Quick Search
    gans settings        open Settings
    gans quit            quit the running instance
    gans --version       print the version and exit
    gans ente-cli://…    passkey redirect from the browser (via the .desktop URL handler)

Everything after the first word is forwarded to the running instance through
``Gtk.Application``'s D-Bus activation, so ``gans toggle`` is what a desktop shortcut binds.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import log
from .version import app_version

COMMANDS = ("toggle", "search", "settings", "quit")


def choose_gdk_backend() -> Optional[str]:
    """Defaults GDK to X11 for tray clipboard ownership and popup placement.
    ``GANS_GDK_BACKEND=wayland`` selects native Wayland; neither toolkit choice enables
    automatic typing in Wayland sessions. Returns the backend chosen, or ``None``."""
    override = os.environ.get("GANS_GDK_BACKEND")
    if override:
        os.environ["GDK_BACKEND"] = override
        return override
    if os.environ.get("GDK_BACKEND"):
        return os.environ["GDK_BACKEND"]
    if os.environ.get("DISPLAY"):
        os.environ["GDK_BACKEND"] = "x11"
        return "x11"
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-V", "version"):
        print(f"gans {app_version()}")
        return 0
    if args and args[0] in ("--help", "-h", "help"):
        print(__doc__.strip())
        return 0
    if args and args[0].startswith("-"):
        print(f"gans: unknown option {args[0]}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if args and args[0] not in COMMANDS and not args[0].startswith("ente-cli://"):
        print(f"gans: unknown command {args[0]!r}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2

    log.configure()
    choose_gdk_backend()

    from .ui.app import GansApplication  # GTK is imported only when we actually run

    application = GansApplication()
    return application.run([sys.argv[0] if sys.argv else "gans"] + args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

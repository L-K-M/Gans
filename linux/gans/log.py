"""Namespaced loggers. We never log secrets, tokens, or codes."""

from __future__ import annotations

import logging
import os

_ROOT = "gans"

app = logging.getLogger(f"{_ROOT}.app")
ente = logging.getLogger(f"{_ROOT}.ente")
hotkey = logging.getLogger(f"{_ROOT}.hotkey")
paste = logging.getLogger(f"{_ROOT}.paste")


def configure() -> None:
    """Installs a stderr handler once. ``GANS_DEBUG=1`` turns on debug output."""
    root = logging.getLogger(_ROOT)
    if root.handlers:
        return
    level = logging.DEBUG if os.environ.get("GANS_DEBUG") else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(name)s: %(levelname)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

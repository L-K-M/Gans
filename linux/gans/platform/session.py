"""What kind of desktop session we're running in, read from the environment.

The answers steer the platform layer: which hotkey backend to use (GNOME's custom
keybindings vs. the portal vs. ``XGrabKey``), whether XTest typing can work at all
(needs an X server — native or XWayland), and how to word the fallbacks. Everything here
is a pure function of ``os.environ`` so it's trivially unit-testable.
"""

from __future__ import annotations

import os

__all__ = ["session_type", "desktop", "is_gnome", "has_x_display"]


# MARK: Session

def session_type() -> str:
    """``"x11"``, ``"wayland"`` or ``"none"``.

    ``XDG_SESSION_TYPE`` is authoritative when it names a graphical session; otherwise
    (``tty`` over SSH with X forwarding, an unset variable under a hand-started X server,
    ``unspecified`` on some display managers) fall back to which display sockets are
    advertised — Wayland first, because a Wayland session normally exports ``DISPLAY``
    for XWayland too."""
    declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if declared in ("x11", "wayland"):
        return declared
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


def has_x_display() -> bool:
    """Whether an X server (native or XWayland) is reachable via ``DISPLAY``."""
    return bool(os.environ.get("DISPLAY"))


# MARK: Desktop

def desktop() -> str:
    """``XDG_CURRENT_DESKTOP`` lower-cased, e.g. ``ubuntu:gnome`` or ``kde``; empty when unset."""
    return os.environ.get("XDG_CURRENT_DESKTOP", "").strip().lower()


def is_gnome() -> bool:
    """True for GNOME and its flavours (``GNOME``, ``ubuntu:GNOME``, ``pop:GNOME``,
    ``GNOME-Classic:GNOME`` …) — the desktops whose keybindings live in
    ``org.gnome.settings-daemon`` and where only a custom shortcut can trigger Gans.

    ``XDG_CURRENT_DESKTOP`` is a colon-separated list (most specific first); any GNOME
    component counts. Older sessions that don't export it are recognised through
    ``DESKTOP_SESSION`` / ``GNOME_DESKTOP_SESSION_ID``."""
    components = [part for part in desktop().split(":") if part]
    if components:
        return any(part.startswith("gnome") for part in components)
    session = os.environ.get("DESKTOP_SESSION", "").strip().lower()
    if session.startswith("gnome") or session in ("ubuntu", "pop"):
        return True
    return bool(os.environ.get("GNOME_DESKTOP_SESSION_ID"))

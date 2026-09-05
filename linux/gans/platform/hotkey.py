"""The global "open Quick Search" hotkey, with **exactly one backend active at a time**.

macOS has a single API for this (Carbon's ``RegisterEventHotKey``, see
``CarbonHotkey.swift``); Linux has none that works everywhere, so ``HotkeyManager`` picks
the first backend the session supports:

1. **GNOME custom keybinding** (X11 and Wayland alike). GNOME Shell offers clients no
   key-grab API, but it lets the user define *custom shortcuts* that run a command, and
   those are plain GSettings. Gans registers "Gans Quick Search" → ``gans toggle`` (with
   the launcher's absolute path, see ``toggle_command``), so a press reaches the running
   instance through ``Gtk.Application``'s D-Bus activation — **not** through
   ``on_pressed``.
2. **XDG GlobalShortcuts portal** — KDE Plasma (Wayland) and any other desktop whose
   portal backend implements ``org.freedesktop.portal.GlobalShortcuts``.
3. **X11 ``XGrabKey``** — classic X11 desktops. Under Wayland (through XWayland) it only
   sees the key while an X11 window is focused, and the status says so.
4. **None** — the user binds ``gans toggle`` in the desktop's keyboard settings.

Because the GNOME shortcut and an X11 grab would both fire on the same press, only one
backend is ever installed, and re-registering tears the previous one down first (like
``CarbonHotkey.register(_:)``, which unregisters before it registers). That holds even
when ``register`` is re-entered: the portal's ``bind`` runs a nested main loop while the
desktop shows its consent dialog, so the Settings recorder can call ``register`` again
before the first call has returned. A registration *generation* detects that; the
superseded call abandons its bind (the desktop drops the dialog) and installs nothing.
Failures are logged and reported through ``HotkeyStatus``; nothing here raises.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from .. import log
from ..hotkeyspec import HotkeySpec

__all__ = ["HotkeyManager", "HotkeyStatus", "manual_instructions", "toggle_command"]

Dispatch = Callable[[Callable[[], object]], object]

#: The command a desktop-level shortcut should run, as the docs name it; it reaches the
#: running instance. ``toggle_command`` resolves it to an absolute path.
TOGGLE_COMMAND = "gans toggle"


def toggle_command() -> Optional[str]:
    """The command a desktop-level shortcut runs to reach this instance, with an absolute
    path — or None when there is nothing invocable to name.

    The desktop spawns the command through *its* environment (gnome-settings-daemon runs
    under ``systemd --user``), not through ours, so a bare ``gans`` only works when it is
    on the session's PATH. The launcher this process was started from is used when it is
    an executable named ``gans`` (``/usr/bin/gans`` for the package, ``linux/bin/gans`` in
    a source-tree run), else whichever ``gans`` is on PATH; either reaches the running
    instance through D-Bus activation. ``python -m gans`` with no ``gans`` on PATH yields
    None, and the GNOME backend then falls through to the others."""
    launcher = _launcher() or shutil.which("gans")
    if not launcher:
        return None
    return f"{shlex.quote(os.path.abspath(launcher))} toggle"


def _launcher() -> Optional[str]:
    """``sys.argv[0]`` when it is an executable ``gans`` launcher (resolved through PATH
    when it is a bare name, like the shell did)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if not argv0:
        return None
    path = argv0 if os.sep in argv0 else shutil.which(argv0)
    if not path:
        return None
    path = os.path.realpath(path)
    if os.path.basename(path) != "gans" or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    return path


def _command_hint() -> str:
    """What to tell the user to bind: the resolved command, else the documented one."""
    return toggle_command() or TOGGLE_COMMAND


def manual_instructions(spec: Optional[HotkeySpec] = None) -> str:
    """What Settings shows when Gans can't register the shortcut itself: the user binds
    ``gans toggle`` in the desktop's own keyboard settings."""
    key = f" (for example {spec.display_string})" if spec is not None else ""
    return (f'To open Quick Search from anywhere, bind the command "{_command_hint()}" to a keyboard '
            f"shortcut{key} in your desktop's keyboard settings (usually Settings → Keyboard → "
            "Custom Shortcuts).")


@dataclass
class HotkeyStatus:
    """Which backend owns the hotkey and how that went — shown verbatim in Settings."""

    #: ``gnome`` | ``portal`` | ``x11`` | ``none``
    backend: str
    ok: bool
    detail: str


_UNREGISTERED = HotkeyStatus("none", False, "No hotkey registered.")


class HotkeyManager:
    """Owns the one active hotkey backend. ``register`` replaces any previous registration;
    ``unregister`` tears it down (including the GNOME custom shortcut, so nothing lingers
    in the user's keyboard settings after Gans quits)."""

    def __init__(self, on_pressed: Callable[[], object], dispatch: Dispatch, x11: Optional[object] = None):
        self._on_pressed = on_pressed
        self._dispatch = dispatch
        self._x11 = x11
        self._spec: Optional[HotkeySpec] = None
        self._status = _UNREGISTERED
        self._gnome_installed = False
        self._portal: Optional[object] = None
        self._grabber: Optional[object] = None
        #: Bumped by every ``unregister`` (and so every ``register``); a ``register`` whose
        #: generation is no longer current has been superseded from a nested main loop.
        self._generation = 0
        #: The portal whose ``bind`` is currently waiting for the desktop's answer.
        self._binding: Optional[object] = None

    @property
    def status(self) -> HotkeyStatus:
        return self._status

    @property
    def spec(self) -> Optional[HotkeySpec]:
        """The most recently requested hotkey (even if no backend could take it)."""
        return self._spec

    def manual_instructions(self) -> str:
        return manual_instructions(self._spec)

    # MARK: Registration

    def register(self, spec: HotkeySpec) -> HotkeyStatus:
        """Registers ``spec`` with the first backend that accepts it, replacing any previous
        registration. Never raises: every failure is logged and lands in the status.

        If this is re-entered (or ``unregister`` is called) while the portal backend waits
        for the desktop's consent dialog, the waiting call yields to the newer one and
        returns the status that call left behind."""
        self.unregister()   # bumps the generation and abandons a bind still waiting on the desktop
        generation = self._generation
        self._spec = spec
        status: Optional[HotkeyStatus] = None
        for backend in (self._try_gnome, self._try_portal, self._try_x11):
            status = backend(spec)
            if self._generation != generation:
                log.hotkey.info("Hotkey %s registration superseded before it finished", spec.display_string)
                return self._status
            if status is not None:
                break
        if status is None:
            status = HotkeyStatus("none", False, manual_instructions(spec))
        self._status = status
        if status.ok:
            log.hotkey.info("Hotkey %s registered via %s", spec.display_string, status.backend)
        else:
            log.hotkey.warning("Hotkey %s not registered: %s", spec.display_string, status.detail)
        return status

    def unregister(self) -> None:
        """Tears down whichever backend is active. Safe to call repeatedly."""
        self._generation += 1
        binding, self._binding = self._binding, None
        if binding is not None:
            binding.close()   # the register() waiting on it returns without installing anything
        if self._gnome_installed:
            self._gnome_installed = False
            try:
                from .gnome import GnomeKeybinding
                GnomeKeybinding.remove()
            except Exception:
                log.hotkey.exception("Couldn't remove the GNOME custom shortcut")
        portal, self._portal = self._portal, None
        if portal is not None:
            portal.close()
        grabber, self._grabber = self._grabber, None
        if grabber is not None:
            try:
                grabber.unregister()
            except Exception:
                log.hotkey.exception("Couldn't release the X11 key grab")
        self._status = _UNREGISTERED

    # MARK: Backends

    def _try_gnome(self, spec: HotkeySpec) -> Optional[HotkeyStatus]:
        try:
            from .session import is_gnome
            if not is_gnome():
                return None
            from .gnome import GnomeKeybinding
            if not GnomeKeybinding.available():
                log.hotkey.info("GNOME session without the media-keys schema; trying other backends")
                return None
            command = toggle_command()
            if command is None:
                log.hotkey.info("No gans launcher to name in a GNOME custom shortcut (none on PATH and not "
                                "started from bin/gans); trying other backends")
                return None
            if not GnomeKeybinding.install(spec, command):
                return None
        except (ImportError, ValueError) as error:   # PyGObject / Gio typelib missing
            log.hotkey.info("GNOME keybinding backend unavailable: %s", error)
            return None
        except Exception:
            log.hotkey.exception("GNOME keybinding backend failed; trying other backends")
            return None
        self._gnome_installed = True
        return HotkeyStatus("gnome", True, (
            f'{spec.display_string} is registered as the GNOME custom shortcut "{GnomeKeybinding.NAME}" '
            f'(Settings → Keyboard → Custom Shortcuts). It runs "{command}", so presses reach Gans '
            "through the desktop rather than a key grab."))

    def _try_portal(self, spec: HotkeySpec) -> Optional[HotkeyStatus]:
        generation = self._generation
        try:
            from .portal import GlobalShortcutsPortal
            portal = GlobalShortcutsPortal(self._on_pressed, self._dispatch)
            # bind() yields to the main loop while the desktop asks for consent; a
            # re-entrant unregister() closes it from there.
            self._binding = portal
            try:
                bound = portal.bind(spec)
            finally:
                if self._binding is portal:
                    self._binding = None
            if not bound:
                return None
        except (ImportError, ValueError) as error:
            log.hotkey.info("GlobalShortcuts portal backend unavailable: %s", error)
            return None
        except Exception:
            log.hotkey.exception("GlobalShortcuts portal backend failed; trying other backends")
            return None
        if self._generation != generation:
            portal.close()   # superseded while the desktop was asked; exactly one backend stays live
            return None
        self._portal = portal
        trigger = getattr(portal, "trigger_description", None)
        bound_as = f" as {trigger}" if trigger else ""
        return HotkeyStatus("portal", True, (
            f"{spec.display_string} is registered through the desktop's GlobalShortcuts portal{bound_as}. "
            "The desktop's settings may let you change the binding."))

    def _try_x11(self, spec: HotkeySpec) -> Optional[HotkeyStatus]:
        x11 = self._x11
        if x11 is None or not getattr(x11, "available", False):
            return None
        try:
            from .session import session_type
            from .x11 import X11HotkeyGrabber
            grabber = X11HotkeyGrabber(x11, self._dispatch)
            if not grabber.register(spec, self._on_pressed):
                return HotkeyStatus("none", False, (
                    f"Couldn't grab {spec.display_string} on the X server — another app may already use it. "
                    f'Try a different combination, or bind "{_command_hint()}" in your desktop\'s keyboard '
                    "settings."))
            wayland = session_type() == "wayland"
        except (ImportError, ValueError) as error:
            log.hotkey.info("X11 hotkey backend unavailable: %s", error)
            return None
        except Exception:
            log.hotkey.exception("X11 hotkey backend failed")
            return None
        self._grabber = grabber
        detail = f"{spec.display_string} is grabbed on the X server."
        if wayland:
            detail += (" This is a Wayland session, so the shortcut only works while an X11 (XWayland) app "
                       f'is focused. For a session-wide shortcut, bind "{_command_hint()}" in your desktop\'s '
                       "keyboard settings instead.")
        return HotkeyStatus("x11", True, detail)

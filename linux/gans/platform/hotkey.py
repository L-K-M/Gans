"""The global "open Quick Search" hotkey, with **exactly one backend active at a time**.

macOS has a single API for this (Carbon's ``RegisterEventHotKey``, see
``CarbonHotkey.swift``); Linux has none that works everywhere, so ``HotkeyManager`` picks
the first backend the session supports:

1. **GNOME custom keybinding** (X11 and Wayland alike). GNOME Shell offers clients no
   key-grab API, but it lets the user define *custom shortcuts* that run a command, and
   those are plain GSettings. Gans registers "Gans Quick Search" → ``gans toggle``, so a
   press reaches the running instance through ``Gtk.Application``'s D-Bus activation —
   **not** through ``on_pressed``.
2. **XDG GlobalShortcuts portal** — KDE Plasma (Wayland) and any other desktop whose
   portal backend implements ``org.freedesktop.portal.GlobalShortcuts``.
3. **X11 ``XGrabKey``** — classic X11 desktops. Under Wayland (through XWayland) it only
   sees the key while an X11 window is focused, and the status says so.
4. **None** — the user binds ``gans toggle`` in the desktop's keyboard settings.

Because the GNOME shortcut and an X11 grab would both fire on the same press, only one
backend is ever installed, and re-registering tears the previous one down first (like
``CarbonHotkey.register(_:)``, which unregisters before it registers). Failures are logged
and reported through ``HotkeyStatus``; nothing here raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .. import log
from ..hotkeyspec import HotkeySpec

__all__ = ["HotkeyManager", "HotkeyStatus", "manual_instructions"]

Dispatch = Callable[[Callable[[], object]], object]

#: The command a desktop-level shortcut should run; it reaches the running instance.
TOGGLE_COMMAND = "gans toggle"


def manual_instructions(spec: Optional[HotkeySpec] = None) -> str:
    """What Settings shows when Gans can't register the shortcut itself: the user binds
    ``gans toggle`` in the desktop's own keyboard settings."""
    key = f" (for example {spec.display_string})" if spec is not None else ""
    return (f'To open Quick Search from anywhere, bind the command "{TOGGLE_COMMAND}" to a keyboard '
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
        registration. Never raises: every failure is logged and lands in the status."""
        self.unregister()
        self._spec = spec
        status = self._try_gnome(spec) or self._try_portal(spec) or self._try_x11(spec)
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
            if not GnomeKeybinding.install(spec, TOGGLE_COMMAND):
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
            f'(Settings → Keyboard → Custom Shortcuts). It runs "{TOGGLE_COMMAND}", so presses reach Gans '
            "through the desktop rather than a key grab."))

    def _try_portal(self, spec: HotkeySpec) -> Optional[HotkeyStatus]:
        try:
            from .portal import GlobalShortcutsPortal
            portal = GlobalShortcutsPortal(self._on_pressed, self._dispatch)
            if not portal.bind(spec):
                return None
        except (ImportError, ValueError) as error:
            log.hotkey.info("GlobalShortcuts portal backend unavailable: %s", error)
            return None
        except Exception:
            log.hotkey.exception("GlobalShortcuts portal backend failed; trying other backends")
            return None
        self._portal = portal
        trigger = getattr(portal, "trigger_description", None)
        bound = f" as {trigger}" if trigger else ""
        return HotkeyStatus("portal", True, (
            f"{spec.display_string} is registered through the desktop's GlobalShortcuts portal{bound}. "
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
                    f'Try a different combination, or bind "{TOGGLE_COMMAND}" in your desktop\'s keyboard '
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
                       f'is focused. For a session-wide shortcut, bind "{TOGGLE_COMMAND}" in your desktop\'s '
                       "keyboard settings instead.")
        return HotkeyStatus("x11", True, detail)

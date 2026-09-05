"""A global hotkey: a GDK key name plus modifier flags. Serializes to the GTK accelerator
form (``<Control><Alt>space``) — the same syntax GNOME's custom keybindings use — and
converts to the forms the other backends need (portal trigger, X11 keysym name)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Optional

__all__ = ["HotkeySpec"]

_MODIFIER_TOKENS = {
    "control": "control", "ctrl": "control", "primary": "control",
    "alt": "alt", "mod1": "alt", "option": "alt",
    "shift": "shift",
    "super": "super_", "mod4": "super_", "meta": "super_", "hyper": "super_",
}

# Display names for the common non-letter keys; anything else is title-cased.
_KEY_DISPLAY = {
    "space": "Space", "return": "Return", "kp_enter": "Enter", "tab": "Tab", "escape": "Esc",
    "backspace": "Backspace", "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
    "page_up": "PageUp", "page_down": "PageDown", "up": "↑", "down": "↓", "left": "←", "right": "→",
    "grave": "`", "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "semicolon": ";", "apostrophe": "'", "comma": ",", "period": ".", "slash": "/", "backslash": "\\",
}


@dataclass(frozen=True)
class HotkeySpec:
    #: GDK key name as ``Gdk.keyval_name`` returns it (``space``, ``a``, ``F1``, ``Return``).
    key: str
    control: bool = False
    alt: bool = False
    shift: bool = False
    super_: bool = False

    DEFAULT: ClassVar["HotkeySpec"]

    @property
    def has_modifier(self) -> bool:
        return self.control or self.alt or self.shift or self.super_

    # MARK: Accelerator form

    @property
    def accelerator(self) -> str:
        """GTK accelerator string, e.g. ``<Control><Alt>space``."""
        parts = []
        if self.control:
            parts.append("<Control>")
        if self.alt:
            parts.append("<Alt>")
        if self.shift:
            parts.append("<Shift>")
        if self.super_:
            parts.append("<Super>")
        return "".join(parts) + self.key

    @classmethod
    def from_accelerator(cls, text: str) -> Optional["HotkeySpec"]:
        """Parses ``<Control><Alt>space`` (case-insensitive modifier names; ``<Primary>`` and
        ``<Mod1>``/``<Mod4>`` aliases accepted). ``None`` if malformed or keyless."""
        if not isinstance(text, str):
            return None
        flags = {"control": False, "alt": False, "shift": False, "super_": False}
        rest = text.strip()
        while True:
            match = re.match(r"^<([A-Za-z0-9]+)>", rest)
            if not match:
                break
            token = _MODIFIER_TOKENS.get(match.group(1).lower())
            if token is None:
                return None
            flags[token] = True
            rest = rest[match.end():]
        key = rest.strip()
        # GDK key names are identifier-like (`space`, `KP_Enter`, `dead_acute`); anything else
        # (e.g. the "Ctrl+Alt+Space" notation) is not a hotkey we can register.
        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            return None
        return cls(key=key, **flags)

    # MARK: Display

    @property
    def display_string(self) -> str:
        """A human-readable form like "Ctrl+Alt+Space" for Settings."""
        parts = []
        if self.control:
            parts.append("Ctrl")
        if self.alt:
            parts.append("Alt")
        if self.shift:
            parts.append("Shift")
        if self.super_:
            parts.append("Super")
        parts.append(self.key_display_name)
        return "+".join(parts)

    @property
    def key_display_name(self) -> str:
        lower = self.key.lower()
        if lower in _KEY_DISPLAY:
            return _KEY_DISPLAY[lower]
        if len(self.key) == 1:
            return self.key.upper()
        if re.fullmatch(r"f[0-9]{1,2}", lower):
            return lower.upper()
        if lower.startswith("kp_"):
            return "Keypad " + self.key[3:].capitalize()
        return self.key[:1].upper() + self.key[1:]

    # MARK: Backend forms

    @property
    def portal_trigger(self) -> str:
        """The XDG GlobalShortcuts ``preferred_trigger`` form: ``CTRL+ALT+space``."""
        parts = []
        if self.control:
            parts.append("CTRL")
        if self.alt:
            parts.append("ALT")
        if self.shift:
            parts.append("SHIFT")
        if self.super_:
            parts.append("LOGO")
        parts.append(self.key)
        return "+".join(parts)

    @property
    def x11_keysym_name(self) -> str:
        """The keysym name for ``Xlib.XK.string_to_keysym`` (GDK and X share the names)."""
        return self.key

    # MARK: JSON

    def to_json(self) -> str:
        return self.accelerator

    @classmethod
    def from_json(cls, value) -> Optional["HotkeySpec"]:
        return cls.from_accelerator(value) if isinstance(value, str) else None


HotkeySpec.DEFAULT = HotkeySpec(key="space", control=True, alt=True)

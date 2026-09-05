"""GNOME's custom keyboard shortcuts, driven through GSettings.

GNOME Shell (X11 and Wayland alike) gives ordinary apps no way to grab a global key —
but it lets the user define *custom shortcuts* that run a command, and those live in
plain GSettings under the relocatable ``…media-keys.custom-keybinding`` schema. Gans
writes one named "Gans Quick Search" that runs ``gans toggle`` (``HotkeyManager`` passes
the launcher's absolute path, since gnome-settings-daemon spawns the command through its
own PATH), which reaches the running instance through ``Gtk.Application``'s D-Bus
activation. The entry shows up (and can be edited or deleted) in Settings → Keyboard →
Custom Shortcuts like any user-created one.

``Gio.Settings`` is used directly rather than the ``gsettings`` CLI so the writes are
typed, land in the user's dconf without spawning a process, and are flushed with
``Gio.Settings.sync()`` before we return. Every entry point checks that both schemas are
installed first: GSettings **aborts the process** on an unknown schema, so ``available()``
is the only safe gate.
"""

from __future__ import annotations

from typing import Optional

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

from .. import log  # noqa: E402
from ..hotkeyspec import HotkeySpec  # noqa: E402

__all__ = ["GnomeKeybinding"]


class GnomeKeybinding:
    SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
    CHILD_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
    LIST_KEY = "custom-keybindings"
    #: Our own slot under the media-keys custom-keybindings tree (must end with ``/``).
    PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/gans/"
    NAME = "Gans Quick Search"
    DEFAULT_COMMAND = "gans toggle"

    # MARK: Availability

    @classmethod
    def available(cls) -> bool:
        """Whether both the media-keys schema and its relocatable custom-keybinding child
        are installed (i.e. gnome-settings-daemon is present)."""
        source = Gio.SettingsSchemaSource.get_default()
        if source is None:
            return False
        return (source.lookup(cls.SCHEMA, True) is not None
                and source.lookup(cls.CHILD_SCHEMA, True) is not None)

    # MARK: Install / remove

    @classmethod
    def install(cls, spec: HotkeySpec, command: str = DEFAULT_COMMAND) -> bool:
        """Creates or updates the shortcut: registers our path in the list of custom
        keybindings (once) and writes name/command/binding. Returns False when the
        schemas aren't installed."""
        if not cls.available():
            log.hotkey.warning("GNOME media-keys schema not installed; can't add a custom shortcut")
            return False
        parent = Gio.Settings.new(cls.SCHEMA)
        paths = list(parent.get_strv(cls.LIST_KEY))
        if cls.PATH not in paths:
            parent.set_strv(cls.LIST_KEY, paths + [cls.PATH])
        child = Gio.Settings.new_with_path(cls.CHILD_SCHEMA, cls.PATH)
        child.set_string("name", cls.NAME)
        child.set_string("command", command)
        child.set_string("binding", spec.accelerator)
        Gio.Settings.sync()
        log.hotkey.info("GNOME custom shortcut %s → %r", spec.display_string, command)
        return True

    @classmethod
    def remove(cls) -> None:
        """Deletes the shortcut: resets its keys and drops our path from the list."""
        if not cls.available():
            return
        child = Gio.Settings.new_with_path(cls.CHILD_SCHEMA, cls.PATH)
        for key in ("name", "command", "binding"):
            child.reset(key)
        parent = Gio.Settings.new(cls.SCHEMA)
        paths = list(parent.get_strv(cls.LIST_KEY))
        if cls.PATH in paths:
            parent.set_strv(cls.LIST_KEY, [path for path in paths if path != cls.PATH])
        Gio.Settings.sync()
        log.hotkey.info("GNOME custom shortcut removed")

    # MARK: Inspection

    @classmethod
    def current(cls) -> Optional[HotkeySpec]:
        """The binding currently installed under our path, or None when absent."""
        if not cls.available():
            return None
        parent = Gio.Settings.new(cls.SCHEMA)
        if cls.PATH not in parent.get_strv(cls.LIST_KEY):
            return None
        child = Gio.Settings.new_with_path(cls.CHILD_SCHEMA, cls.PATH)
        return HotkeySpec.from_accelerator(child.get_string("binding"))

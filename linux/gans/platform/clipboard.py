"""The system clipboard, with the one-time-code hygiene of ``CodeInjector.copyToClipboard``:
password-manager hints so clipboard history tools skip the code, no clipboard-manager
persistence, and a clear-after timer that only fires if the code is still the most
recent thing on the clipboard.

GTK's ``gtk_clipboard_set_with_data`` (custom targets + a "someone else took over"
callback) isn't exposed through introspection, so this does what ``GtkClipboard`` does
underneath: a hidden ``GtkInvisible`` owns the ``CLIPBOARD`` selection through
``gtk_selection_owner_set`` and serves requests from its ``selection-get`` signal, while
``selection-clear-event`` tells us the moment another owner appears — the equivalent of
comparing ``NSPasteboard.changeCount``. GTK is imported lazily so the class is
constructible in a process without a display; ``copy`` then just reports failure.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .. import log

__all__ = ["Clipboard"]

#: Klipper (KDE) skips items carrying this target; GNOME's history extensions honour
#: it too. macOS uses ``org.nspasteboard.ConcealedType`` for the same purpose.
PASSWORD_HINT_TARGET = "x-kde-passwordManagerHint"
_TEXT_TARGETS = ("UTF8_STRING", "TEXT", "STRING", "text/plain;charset=utf-8", "text/plain")

_toolkit_cache: Optional[Tuple[object, object, object]] = None


def _toolkit() -> Optional[Tuple[object, object, object]]:
    """``(Gtk, Gdk, GLib)`` — or None when PyGObject/GTK 3 isn't importable."""
    global _toolkit_cache
    if _toolkit_cache is None:
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk, GLib, Gtk
        except (ImportError, ValueError) as error:
            log.paste.warning("GTK is unavailable; the clipboard can't be used: %s", error)
            return None
        _toolkit_cache = (Gtk, Gdk, GLib)
    return _toolkit_cache


class Clipboard:
    """Owns the ``CLIPBOARD`` selection on behalf of the app. One instance per app."""

    def __init__(self) -> None:
        self._owner = None            # the Gtk.Invisible that owns the selection
        self._text: Optional[str] = None  # what we're serving; None once ownership is lost
        self._generation = 0          # bumps per copy — the changeCount stand-in

    # MARK: Copy

    def copy(self, text: str, clear_after: Optional[float] = None) -> bool:
        """Places ``text`` on the clipboard. With ``clear_after`` (seconds) the code is
        wiped later — but only if the clipboard hasn't changed since, so we never clobber
        something the user copied afterwards. Returns False when there's no display."""
        owner = self._owner_widget()
        if owner is None:
            return False
        Gtk, Gdk, GLib = _toolkit()
        self._generation += 1
        generation = self._generation
        self._text = text
        if not Gtk.selection_owner_set(owner, Gdk.SELECTION_CLIPBOARD, Gdk.CURRENT_TIME):
            self._text = None
            log.paste.warning("Couldn't take ownership of the clipboard")
            return False
        if clear_after is not None and clear_after > 0:
            seconds = float(clear_after)
            if seconds.is_integer():
                GLib.timeout_add_seconds(int(seconds), self._clear_after_timeout, generation)
            else:
                GLib.timeout_add(int(seconds * 1000), self._clear_after_timeout, generation)
        return True

    def clear_if_still(self, text: str) -> bool:
        """Clears the clipboard if it still holds ``text`` *from us*. Returns whether it did."""
        if self._text is None or self._text != text:
            return False
        return self._disown()

    def release(self) -> None:
        """Gives the selection up and drops the owner widget (app shutdown, or tests that
        close the GDK display)."""
        self._disown()
        if self._owner is not None:
            self._owner.destroy()
            self._owner = None

    # MARK: Ownership

    def _owner_widget(self):
        if self._owner is not None:
            return self._owner
        toolkit = _toolkit()
        if toolkit is None:
            return None
        Gtk, Gdk, _ = toolkit
        if Gdk.Display.get_default() is None:
            log.paste.warning("GTK has no display; the clipboard can't be used")
            return None
        invisible = Gtk.Invisible()
        invisible.show()  # realizes the (input-only) window that owns the selection
        targets = [Gtk.TargetEntry.new(name, 0, index) for index, name in enumerate(_TEXT_TARGETS)]
        targets.append(Gtk.TargetEntry.new(PASSWORD_HINT_TARGET, 0, len(_TEXT_TARGETS)))
        Gtk.selection_add_targets(invisible, Gdk.SELECTION_CLIPBOARD, targets)
        invisible.connect("selection-get", self._on_selection_get)
        invisible.connect("selection-clear-event", self._on_selection_clear)
        self._owner = invisible
        return invisible

    def _on_selection_get(self, widget, selection_data, info, time) -> None:
        target = selection_data.get_target()
        if target.name() == PASSWORD_HINT_TARGET:
            selection_data.set(target, 8, b"secret")
        else:
            selection_data.set_text(self._text or "", -1)

    def _on_selection_clear(self, widget, event) -> bool:
        """Another owner took the selection (or we gave it up): the code is no longer ours
        to clear."""
        self._text = None
        return False  # let GTK's default handler tidy its selection bookkeeping

    def _clear_after_timeout(self, generation: int) -> bool:
        if generation == self._generation:
            self._disown()
        return False  # one-shot GLib source

    def _disown(self) -> bool:
        """Empties the clipboard if we still own it. Ownership is re-checked with the
        server: a foreign owner's ``SelectionClear`` may still be in flight."""
        if self._text is None or self._owner is None:
            return False
        Gtk, Gdk, _ = _toolkit()
        self._text = None
        if Gdk.selection_owner_get(Gdk.SELECTION_CLIPBOARD) != self._owner.get_window():
            return False
        Gtk.selection_owner_set(None, Gdk.SELECTION_CLIPBOARD, Gdk.CURRENT_TIME)
        return True

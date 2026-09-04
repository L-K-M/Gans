"""The tray item — the ``NSStatusItem`` port. Owns the panel glyph and its menu: each entry
row copies that entry's current code to the clipboard; the footer exposes Quick Search,
refresh, settings, updates, account, and quit.

Backends, in order: Ayatana AppIndicator → the legacy AppIndicator → ``Gtk.StatusIcon``.
The AppIndicator (StatusNotifierItem) route is what Ubuntu GNOME, KDE, XFCE, Cinnamon
and MATE render; the host panel draws the icon and menu itself from D-Bus. That has one
structural consequence versus AppKit: the menu can't be built lazily in a
``menuNeedsUpdate`` callback when it opens — it's exported ahead of time — so it is
rebuilt whenever the vault or the lock state changes. Without an SNI host (no panel, a
bare session bus) the indicator simply never shows; nothing here depends on one.
"""

from __future__ import annotations

import importlib
import threading
from functools import partial
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .. import log  # noqa: E402
from ..ente.vault import VaultState  # noqa: E402
from ..entry import AuthEntry  # noqa: E402

__all__ = ["StatusItemController", "Icon", "ICON_DIR", "INDICATOR_ID", "FLASH_MILLISECONDS"]

#: The symbolic tray icons live in the source tree so the app works uninstalled; the
#: package also installs them into the hicolor theme.
ICON_DIR = Path(__file__).resolve().parent.parent / "data" / "icons"
INDICATOR_ID = "ch.lkmc.Gans"
#: How long the copied/honk glyph shows before the key (or padlock) returns.
FLASH_MILLISECONDS = 900

Handler = Callable[[], object]


class Icon:
    """The tray glyphs (icon-theme names of the SVGs in ``ICON_DIR``)."""

    KEY = "gans-tray-symbolic"            # resting state — the app's glyph
    LOCKED = "gans-tray-locked-symbolic"  # the app lock is engaged
    COPIED = "gans-tray-copied-symbolic"  # a code was just copied / typed
    HONK = "gans-tray-honk-symbolic"      # …the same, in honk mode 🪿


# MARK: Backends

_INDICATOR_NAMESPACES: Tuple[Tuple[str, str], ...] = (
    ("AyatanaAppIndicator3", "ayatana-appindicator"),
    ("AppIndicator3", "appindicator"),
)


def _load_indicator_module() -> Tuple[Optional[str], Optional[ModuleType]]:
    """``(backend name, gi module)`` for the first AppIndicator typelib present, else
    ``(None, None)``."""
    for namespace, backend in _INDICATOR_NAMESPACES:
        try:
            gi.require_version(namespace, "0.1")
            module = importlib.import_module(f"gi.repository.{namespace}")
        except (ImportError, ValueError) as error:
            log.app.debug("%s is unavailable: %s", namespace, error)
            continue
        return backend, module
    return None, None


class _IndicatorBackend:
    """StatusNotifierItem through libappindicator: the panel renders the icon (looked up
    by name in our theme path) and the menu (exported over dbusmenu)."""

    def __init__(self, module: ModuleType, menu: Gtk.Menu):
        self._indicator = module.Indicator.new(INDICATOR_ID, Icon.KEY, module.IndicatorCategory.APPLICATION_STATUS)
        self._indicator.set_icon_theme_path(str(ICON_DIR))
        self._indicator.set_title("Gans")
        self._indicator.set_menu(menu)
        self._indicator.set_status(module.IndicatorStatus.ACTIVE)

    def set_icon(self, name: str, description: str) -> None:
        self._indicator.set_icon_full(name, description)


class _StatusIconBackend:
    """The legacy XEmbed tray (``Gtk.StatusIcon``) for desktops without an SNI host. GTK
    renders the icon in-process, so the icon directory joins the default theme's search
    path; a left click opens Quick Search and a right click pops the menu at the pointer."""

    def __init__(self, menu: Gtk.Menu, on_activate: Handler):
        Gtk.IconTheme.get_default().append_search_path(str(ICON_DIR))
        self._menu = menu
        self._icon = Gtk.StatusIcon.new_from_icon_name(Icon.KEY)
        self._icon.set_title("Gans")
        self._icon.set_tooltip_text("Gans")
        self._icon.connect("activate", lambda _icon: on_activate())
        self._icon.connect("popup-menu", self._on_popup_menu)

    def _on_popup_menu(self, _icon, _button: int, _activate_time: int) -> None:
        self._menu.popup_at_pointer(None)

    def set_icon(self, name: str, description: str) -> None:
        self._icon.set_from_icon_name(name)
        self._icon.set_tooltip_text(description)


# MARK: Controller

class StatusItemController:
    """Owns the tray item. ``menu`` is the live ``Gtk.Menu`` (rebuilt in place so the
    exported dbusmenu stays valid); ``backend`` names the tray protocol in use
    (``ayatana-appindicator`` / ``appindicator`` / ``statusicon``)."""

    def __init__(self, vault, prefs, app_lock,
                 on_quick_search: Handler, on_settings: Handler, on_login: Handler,
                 on_check_for_updates: Handler, on_unlock: Handler, on_quit: Handler,
                 clipboard=None, honk=None):
        self._vault = vault
        self._prefs = prefs
        self._app_lock = app_lock
        self._on_quick_search = on_quick_search
        self._on_settings = on_settings
        self._on_login = on_login
        self._on_check_for_updates = on_check_for_updates
        self._on_unlock = on_unlock
        self._on_quit = on_quit
        self._clipboard = clipboard
        self._honk = honk

        #: Monotonic token so overlapping flashes can't restore a stale glyph (two quick
        #: copies used to capture the checkmark as "original" and leave it stuck).
        self._flash_generation = 0
        self._icon_name = Icon.KEY

        self.menu = Gtk.Menu()
        backend, module = _load_indicator_module()
        if module is not None:
            self._backend = _IndicatorBackend(module, self.menu)
            self.backend = backend
        else:
            self._backend = _StatusIconBackend(self.menu, self._on_quick_search)
            self.backend = "statusicon"
        log.app.debug("Tray backend: %s", self.backend)

        self._configure_icon()
        self.rebuild()
        # Reflect vault and lock changes in the menu (and the lock in the glyph).
        vault.on_change(self.rebuild)
        app_lock.on_change(self._on_lock_changed)

    # MARK: Icon

    @property
    def icon_name(self) -> str:
        """The glyph currently shown (one of ``Icon``'s names)."""
        return self._icon_name

    def _set_icon(self, name: str, description: str) -> None:
        self._icon_name = name
        self._backend.set_icon(name, description)

    def _configure_icon(self) -> None:
        """Draws the glyph for the current lock state."""
        self._set_icon(Icon.LOCKED if self._app_lock.is_locked else Icon.KEY, "Gans")

    def _on_lock_changed(self) -> None:
        self._configure_icon()
        self.rebuild()

    # MARK: Menu

    def rebuild(self) -> None:
        """Rebuilds the menu from the current state — the ``menuNeedsUpdate`` equivalent,
        run eagerly because the SNI host holds a copy."""
        for child in self.menu.get_children():
            child.destroy()
        if self._app_lock.is_locked:
            self._build_locked_menu()
        elif self._vault.is_signed_in:
            self._build_signed_in_menu()
        else:
            self._build_signed_out_menu()
        self.menu.show_all()

    def _build_locked_menu(self) -> None:
        self._add_item("Gans is locked")
        self._add_item("Unlock Gans…", self._on_unlock)
        self._add_separator()
        self._add_common_footer()

    def _build_signed_out_menu(self) -> None:
        self._add_item("Sign in to Ente…", self._on_login)
        self._add_separator()
        self._add_common_footer()

    def _build_signed_in_menu(self) -> None:
        email = self._vault.account_email
        if email:
            self._add_item(email)
        if not self._vault.keyring_persistent:
            # No Secret Service: the session lives in memory and is gone at the next
            # launch. Say so where the user will look for their account.
            self._add_item("Session not saved (no keyring)")

        # A dead token would otherwise fail silently while stale cached codes keep
        # showing — surface it and offer the fix right here.
        if self._vault.session_expired:
            self._add_item("⚠️ Session expired — codes no longer sync")
            self._add_item("Sign In Again…", self._on_login)
            self._add_separator()

        entries = list(self._vault.entries)
        state = self._vault.state
        if state is VaultState.LOADING and not entries:
            self._add_item("Syncing…")
        elif state is VaultState.ERROR and not entries:
            self._add_item(self._vault.error_message or "Sync failed")
        elif not entries:
            self._add_item("No entries")
        else:
            # Show every entry — name only (never the live code) — and copy it on click.
            # The list is intentionally not truncated: a subset would leave the rest
            # unreachable from the menu. An over-long GtkMenu (and the SNI host's copy)
            # scrolls on its own, and Quick Search is the fast path for large vaults,
            # so a complete menu costs nothing but stays exhaustive.
            for entry in entries:
                self._add_item(entry.display_name, partial(self.copy, entry))

        self._add_separator()
        self._add_item("Quick Search…", self._on_quick_search)
        self._add_item("Refresh Now", self._refresh)
        self._add_item("Lock Now", self._app_lock.lock)
        self._add_item("Sign Out", self._vault.sign_out)
        self._add_separator()
        self._add_common_footer()  # Settings, Check for Updates, then Quit last.

    def _add_common_footer(self) -> None:
        self._add_item("Settings…", self._on_settings)
        self._add_item("Check for Updates…", self._on_check_for_updates)
        self._add_separator()
        self._add_item("Quit Gans", self._on_quit)

    def _add_item(self, title: str, handler: Optional[Handler] = None) -> Gtk.MenuItem:
        """Appends a row; without a handler it's an insensitive header/status line.
        ``new_with_label`` keeps underscores in entry names literal (no mnemonics)."""
        item = Gtk.MenuItem.new_with_label(title)
        if handler is None:
            item.set_sensitive(False)
        else:
            item.connect("activate", lambda _item: handler())
        self.menu.append(item)
        return item

    def _add_separator(self) -> None:
        self.menu.append(Gtk.SeparatorMenuItem())

    # MARK: Actions

    def _refresh(self) -> None:
        """"Refresh Now": the sync is network-bound, so it runs off the main loop; the
        vault reports back through its observers."""
        def run() -> None:
            try:
                self._vault.refresh()
            except Exception:
                log.ente.exception("Refresh failed")
        threading.Thread(target=run, name="gans-refresh", daemon=True).start()

    def copy(self, entry: AuthEntry) -> None:
        """Copies ``entry``'s current code (with the configured clear-after delay), records
        the use for frecency, and confirms."""
        if self._clipboard is not None:
            self._clipboard.copy(entry.code(), clear_after=self._prefs.clipboard_clear_delay)
        else:
            log.app.debug("No clipboard is wired to the tray; nothing copied")
        self._prefs.record_usage(entry.id)
        self.confirm_copy()

    def confirm_copy(self) -> None:
        """Confirms a copy or Quick Search commit: briefly swaps the tray glyph — a goose
        in honk mode, otherwise a checkmark — and, in honk mode, plays the honk. Public so
        the Quick Search commit path can trigger the same confirmation."""
        honk = self._prefs.honk_on_copy
        if honk and self._honk is not None:
            self._honk.play()
        self._flash_generation += 1
        generation = self._flash_generation
        self._set_icon(Icon.HONK if honk else Icon.COPIED, "Copied")
        GLib.timeout_add(FLASH_MILLISECONDS, self._end_flash, generation)

    def _end_flash(self, generation: int) -> bool:
        if generation == self._flash_generation:
            # Redraw from the current state instead of a captured glyph, so a lock-state
            # change during the flash can't be clobbered either.
            self._configure_icon()
        return False  # one-shot GLib source

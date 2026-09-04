"""The tray item on the shared Xvfb display + private session bus (no SNI host, so the
AppIndicator is exported to nobody — exactly the "no panel" case it must survive): backend
selection, the menu for every vault/lock state, the row actions, the copy confirmation
flash, and the ``Gtk.StatusIcon`` fallback. The vault, lock, clipboard and honk are fakes.

One controller per test class: libappindicator exports the indicator at a D-Bus path
derived from its (fixed) id, so a second live indicator in the same process would only
collide with the first.
"""

import collections
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.harness import gtk_available, pump, wait_until

from gans.ente.vault import VaultState
from gans.entry import AuthEntry
from gans.prefs import Preferences
from gans.ui.tray import FLASH_MILLISECONDS, ICON_DIR, INDICATOR_ID, Icon, StatusItemController

SECRET = "JBSWY3DPEHPK3PXP"
EMAIL = "alice@example.com"
ICON_NAMES = (Icon.KEY, Icon.LOCKED, Icon.COPIED, Icon.HONK)
INDICATOR_BACKENDS = ("ayatana-appindicator", "appindicator")


def make_entries(count: int = 3):
    entries = []
    for index in range(count):
        uri = f"otpauth://totp/Service%20{index:02d}:alice?secret={SECRET}&issuer=Service%20{index:02d}"
        entries.append(AuthEntry.parse(uri, f"id-{index}"))
    assert all(entries)
    return entries


def hotp_entry():
    """An HOTP entry: its code depends on the counter only, so a copy can be checked
    without racing a TOTP window."""
    entry = AuthEntry.parse(f"otpauth://hotp/Bank:alice?secret={SECRET}&issuer=Bank&counter=5", "bank")
    assert entry is not None
    return entry


# MARK: Fakes

class FakeVault:
    def __init__(self):
        self._observers = []
        self.reset()

    def reset(self):
        self.entries = []
        self.state = VaultState.SIGNED_OUT
        self.error_message = ""
        self.account_email = None
        self.session_expired = False
        self.is_signed_in = False
        self.keyring_persistent = True
        self.refresh_threads = []
        self.sign_out_calls = 0
        self.notify()

    def on_change(self, callback):
        self._observers.append(callback)

    def notify(self):
        for callback in list(self._observers):
            callback()

    def sign_in(self, entries=(), state=VaultState.READY, email=EMAIL):
        self.is_signed_in = True
        self.account_email = email
        self.entries = list(entries)
        self.state = state
        self.notify()

    def refresh(self):
        self.refresh_threads.append(threading.current_thread().name)

    def sign_out(self):
        self.reset()
        self.sign_out_calls += 1


class FakeAppLock:
    def __init__(self):
        self._observers = []
        self.is_locked = False

    def on_change(self, callback):
        self._observers.append(callback)

    def set_locked(self, locked):
        self.is_locked = locked
        for callback in list(self._observers):
            callback()

    def lock(self):
        self.set_locked(True)


class FakeClipboard:
    def __init__(self):
        self.copies = []

    def copy(self, text, clear_after=None):
        self.copies.append((text, clear_after))
        return True


class FakeHonk:
    plays = 0

    @classmethod
    def play(cls):
        cls.plays += 1


def labels(menu):
    from gi.repository import Gtk
    return ["—" if isinstance(child, Gtk.SeparatorMenuItem) else child.get_label() for child in menu.get_children()]


def row(menu, label):
    from gi.repository import Gtk
    for child in menu.get_children():
        if not isinstance(child, Gtk.SeparatorMenuItem) and child.get_label() == label:
            return child
    raise AssertionError(f"no menu row {label!r} in {labels(menu)}")


FOOTER = ["Settings…", "Check for Updates…", "—", "Quit Gans"]


class TrayTestCase(unittest.TestCase):
    """Shared fixture: fakes, a temp ``Preferences`` and one controller for the class."""

    @classmethod
    def setUpClass(cls):
        from tests.gtkbind import gtk_session
        gtk_session()
        cls.directory = tempfile.TemporaryDirectory()
        cls.prefs = Preferences(Path(cls.directory.name) / "preferences.json")
        cls.vault = FakeVault()
        cls.lock = FakeAppLock()
        cls.clipboard = FakeClipboard()
        cls.calls = collections.Counter()
        callbacks = {name: (lambda name=name: cls.calls.update([name]))
                     for name in ("quick_search", "settings", "login", "check_for_updates", "unlock", "quit")}
        cls.controller = cls.build_controller(callbacks)

    @classmethod
    def build_controller(cls, callbacks):
        return StatusItemController(
            cls.vault, cls.prefs, cls.lock,
            on_quick_search=callbacks["quick_search"], on_settings=callbacks["settings"],
            on_login=callbacks["login"], on_check_for_updates=callbacks["check_for_updates"],
            on_unlock=callbacks["unlock"], on_quit=callbacks["quit"],
            clipboard=cls.clipboard, honk=FakeHonk)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.lock.set_locked(False)
        self.vault.reset()
        self.clipboard.copies.clear()
        self.calls.clear()
        FakeHonk.plays = 0
        self.prefs.honk_on_copy = False
        self.prefs.clear_clipboard_enabled = True
        pump(10)

    @property
    def menu(self):
        return self.controller.menu


# MARK: AppIndicator backend

@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class StatusItemControllerTests(TrayTestCase):
    def test_backend_is_an_appindicator_configured_for_the_panel(self):
        from gans.ui.tray import _load_indicator_module
        backend, module = _load_indicator_module()
        if module is None:
            self.skipTest("no AppIndicator typelib installed")
        self.assertEqual(self.controller.backend, backend)
        self.assertIn(backend, INDICATOR_BACKENDS)
        indicator = self.controller._backend._indicator
        self.assertEqual(indicator.get_id(), INDICATOR_ID)
        self.assertEqual(indicator.get_icon_theme_path(), str(ICON_DIR))
        self.assertEqual(int(indicator.get_status()), int(module.IndicatorStatus.ACTIVE))
        self.assertEqual(int(indicator.get_category()), int(module.IndicatorCategory.APPLICATION_STATUS))
        self.assertEqual(indicator.get_title(), "Gans")
        self.assertIs(indicator.get_menu(), self.menu)
        self.assertEqual(indicator.get_icon(), Icon.KEY)
        self.assertFalse(indicator.get_property("connected"))   # no SNI host on the private bus

    def test_signed_out_menu(self):
        self.assertEqual(labels(self.menu), ["Sign in to Ente…", "—"] + FOOTER)
        row(self.menu, "Sign in to Ente…").activate()
        row(self.menu, "Settings…").activate()
        row(self.menu, "Check for Updates…").activate()
        row(self.menu, "Quit Gans").activate()
        self.assertEqual(self.calls, {"login": 1, "settings": 1, "check_for_updates": 1, "quit": 1})
        self.assertEqual(self.controller.icon_name, Icon.KEY)

    def test_locked_menu_and_glyph(self):
        self.vault.sign_in(make_entries())
        self.lock.lock()
        self.assertEqual(self.controller.icon_name, Icon.LOCKED)
        self.assertEqual(labels(self.menu), ["Gans is locked", "Unlock Gans…", "—"] + FOOTER)
        self.assertFalse(row(self.menu, "Gans is locked").get_sensitive())
        row(self.menu, "Unlock Gans…").activate()
        self.assertEqual(self.calls, {"unlock": 1})

        self.lock.set_locked(False)
        self.assertEqual(self.controller.icon_name, Icon.KEY)
        self.assertEqual(labels(self.menu)[0], EMAIL)   # back to the signed-in menu

    def test_signed_in_menu(self):
        entries = make_entries(3)
        self.vault.sign_in(entries)
        self.assertEqual(labels(self.menu),
                         [EMAIL] + [entry.display_name for entry in entries]
                         + ["—", "Quick Search…", "Refresh Now", "Lock Now", "Sign Out", "—"] + FOOTER)
        self.assertFalse(row(self.menu, EMAIL).get_sensitive())
        for entry in entries:
            self.assertTrue(row(self.menu, entry.display_name).get_sensitive())
        self.assertEqual(self.controller.icon_name, Icon.KEY)

    def test_every_entry_is_listed_in_order(self):
        entries = make_entries(80)
        self.vault.sign_in(entries)
        names = labels(self.menu)[1:81]
        self.assertEqual(names, [entry.display_name for entry in entries])
        self.assertEqual(labels(self.menu)[81], "—")

    def test_vault_state_rows(self):
        self.vault.sign_in([], state=VaultState.LOADING)
        self.assertEqual(labels(self.menu)[:3], [EMAIL, "Syncing…", "—"])
        self.assertFalse(row(self.menu, "Syncing…").get_sensitive())

        self.vault.state = VaultState.ERROR
        self.vault.error_message = "Network error: unreachable"
        self.vault.notify()
        self.assertEqual(labels(self.menu)[:3], [EMAIL, "Network error: unreachable", "—"])
        self.assertFalse(row(self.menu, "Network error: unreachable").get_sensitive())

        self.vault.sign_in([], state=VaultState.READY)
        self.assertEqual(labels(self.menu)[:3], [EMAIL, "No entries", "—"])

        entries = make_entries(2)   # cached entries show even while a sync is running
        self.vault.sign_in(entries, state=VaultState.LOADING)
        self.assertEqual(labels(self.menu)[:4], [EMAIL, entries[0].display_name, entries[1].display_name, "—"])

    def test_session_expired_and_memory_only_session_rows(self):
        entries = make_entries(1)
        self.vault.sign_in(entries)
        self.vault.session_expired = True
        self.vault.keyring_persistent = False
        self.vault.notify()
        self.assertEqual(labels(self.menu)[:6],
                         [EMAIL, "Session not saved (no keyring)", "⚠️ Session expired — codes no longer sync",
                          "Sign In Again…", "—", entries[0].display_name])
        self.assertFalse(row(self.menu, "Session not saved (no keyring)").get_sensitive())
        self.assertFalse(row(self.menu, "⚠️ Session expired — codes no longer sync").get_sensitive())
        row(self.menu, "Sign In Again…").activate()
        self.assertEqual(self.calls, {"login": 1})

    def test_no_email_means_no_header(self):
        self.vault.sign_in(make_entries(1), email=None)
        self.assertEqual(labels(self.menu)[0], "Service 00 (alice)")

    def test_signed_in_actions(self):
        self.vault.sign_in(make_entries(1))
        row(self.menu, "Quick Search…").activate()
        self.assertEqual(self.calls, {"quick_search": 1})

        row(self.menu, "Refresh Now").activate()
        self.assertTrue(wait_until(lambda: len(self.vault.refresh_threads) == 1))
        self.assertEqual(self.vault.refresh_threads, ["gans-refresh"])   # off the main loop

        row(self.menu, "Lock Now").activate()
        self.assertTrue(self.lock.is_locked)
        self.assertEqual(labels(self.menu)[0], "Gans is locked")
        self.lock.set_locked(False)

        row(self.menu, "Sign Out").activate()
        self.assertEqual(self.vault.sign_out_calls, 1)
        self.assertEqual(labels(self.menu)[0], "Sign in to Ente…")

    def test_entry_row_copies_records_usage_and_flashes(self):
        entry = hotp_entry()
        self.vault.sign_in([entry])
        uses_before = self.prefs.usage_counts.get("bank", 0)
        started = time.monotonic()
        row(self.menu, "Bank (alice)").activate()
        self.assertEqual(self.clipboard.copies, [(entry.code(), 30.0)])
        self.assertEqual(self.prefs.recently_used_ids[0], "bank")
        self.assertEqual(self.prefs.usage_counts["bank"], uses_before + 1)
        self.assertEqual(self.controller.icon_name, Icon.COPIED)
        self.assertEqual(FakeHonk.plays, 0)
        self.assertTrue(wait_until(lambda: self.controller.icon_name == Icon.KEY, timeout=3))
        self.assertGreaterEqual(time.monotonic() - started, FLASH_MILLISECONDS / 1000 - 0.1)

    def test_clear_after_follows_the_preference(self):
        entry = hotp_entry()
        self.vault.sign_in([entry])
        self.prefs.clear_clipboard_enabled = False
        row(self.menu, "Bank (alice)").activate()
        self.assertEqual(self.clipboard.copies, [(entry.code(), None)])
        self.assertTrue(wait_until(lambda: self.controller.icon_name == Icon.KEY, timeout=3))

    def test_honk_mode_plays_and_shows_the_goose(self):
        self.vault.sign_in([hotp_entry()])
        self.prefs.honk_on_copy = True
        row(self.menu, "Bank (alice)").activate()
        self.assertEqual(FakeHonk.plays, 1)
        self.assertEqual(self.controller.icon_name, Icon.HONK)
        self.assertTrue(wait_until(lambda: self.controller.icon_name == Icon.KEY, timeout=3))

    def test_overlapping_flashes_restore_the_glyph_once(self):
        self.controller.confirm_copy()
        pump(400)
        self.controller.confirm_copy()
        pump(650)   # the first flash's timer has fired — and must have been ignored
        self.assertEqual(self.controller.icon_name, Icon.COPIED)
        self.assertTrue(wait_until(lambda: self.controller.icon_name == Icon.KEY, timeout=2))

    def test_lock_change_during_a_flash_is_not_clobbered(self):
        history = []
        original = self.controller._backend.set_icon
        with patch.object(self.controller._backend, "set_icon",
                          side_effect=lambda name, description: (history.append(name), original(name, description))):
            self.controller.confirm_copy()
            self.lock.lock()
            self.assertEqual(self.controller.icon_name, Icon.LOCKED)
            pump(FLASH_MILLISECONDS + 200)
        self.assertEqual(history, [Icon.COPIED, Icon.LOCKED, Icon.LOCKED])   # never back to the key
        self.assertEqual(self.controller.icon_name, Icon.LOCKED)


# MARK: Gtk.StatusIcon fallback

@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class StatusIconFallbackTests(TrayTestCase):
    @classmethod
    def build_controller(cls, callbacks):
        with patch("gans.ui.tray._load_indicator_module", return_value=(None, None)):
            controller = StatusItemController(
                cls.vault, cls.prefs, cls.lock,
                on_quick_search=callbacks["quick_search"], on_settings=callbacks["settings"],
                on_login=callbacks["login"], on_check_for_updates=callbacks["check_for_updates"],
                on_unlock=callbacks["unlock"], on_quit=callbacks["quit"])   # no clipboard / honk wired
        return controller

    def test_backend_is_a_status_icon_with_the_icons_in_the_theme(self):
        from gi.repository import Gtk
        self.assertEqual(self.controller.backend, "statusicon")
        self.assertIsInstance(self.controller._backend._icon, Gtk.StatusIcon)
        theme = Gtk.IconTheme.get_default()
        for name in ICON_NAMES:   # unthemed icons in a search path: found by lookup, not by has_icon
            self.assertIsNotNone(theme.lookup_icon(name, 16, 0), name)
        self.assertEqual(self.controller._backend._icon.get_icon_name(), Icon.KEY)
        self.assertEqual(self.controller._backend._icon.get_title(), "Gans")

    def test_left_click_opens_quick_search(self):
        self.controller._backend._icon.emit("activate")
        self.assertEqual(self.calls, {"quick_search": 1})

    def test_right_click_pops_the_menu(self):
        from gi.repository import Gtk
        self.vault.sign_in(make_entries(2))
        self.controller._backend._icon.emit("popup-menu", 3, Gtk.get_current_event_time())
        try:
            self.assertTrue(wait_until(lambda: self.menu.get_visible() and self.menu.get_mapped(), timeout=3))
            self.assertEqual(labels(self.menu)[1], "Service 00 (alice)")
        finally:
            self.menu.popdown()
            pump(50)
        self.assertFalse(self.menu.get_visible())

    def test_glyph_changes_reach_the_status_icon(self):
        icon = self.controller._backend._icon
        self.lock.lock()
        self.assertEqual(icon.get_icon_name(), Icon.LOCKED)
        self.lock.set_locked(False)
        self.controller.confirm_copy()
        self.assertEqual(icon.get_icon_name(), Icon.COPIED)
        self.assertEqual(icon.get_tooltip_text(), "Copied")
        self.assertTrue(wait_until(lambda: icon.get_icon_name() == Icon.KEY, timeout=3))
        self.assertEqual(icon.get_tooltip_text(), "Gans")

    def test_copy_without_a_clipboard_still_records_and_confirms(self):
        self.vault.sign_in([hotp_entry()])
        with self.assertLogs("gans.app", level="DEBUG") as logs:
            row(self.menu, "Bank (alice)").activate()
        self.assertIn("No clipboard", logs.output[0])
        self.assertEqual(self.prefs.recently_used_ids[0], "bank")
        self.assertEqual(self.controller.icon_name, Icon.COPIED)
        self.assertTrue(wait_until(lambda: self.controller.icon_name == Icon.KEY, timeout=3))


# MARK: Icon artwork

def svg_loader_available() -> bool:
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return False
    return any(fmt.get_name() == "svg" for fmt in GdkPixbuf.Pixbuf.get_formats())


def opaque_pixels(pixbuf):
    """``(count, all-white)`` over the pixels with alpha > 0."""
    assert pixbuf.get_has_alpha() and pixbuf.get_n_channels() == 4
    data, stride = pixbuf.get_pixels(), pixbuf.get_rowstride()
    count, white = 0, True
    for y in range(pixbuf.get_height()):
        for x in range(pixbuf.get_width()):
            offset = y * stride + x * 4
            if data[offset + 3] > 0:
                count += 1
                white = white and data[offset:offset + 3] == b"\xff\xff\xff"
    return count, white


class TrayIconTests(unittest.TestCase):
    def test_icons_are_adwaita_style_symbolic_svgs(self):
        for name in ICON_NAMES:
            source = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8")
            self.assertIn('viewBox="0 0 16 16"', source, name)
            self.assertIn('fill="#2e3436"', source, name)
            for forbidden in ("stroke", "<image", "<text", "<style", 'fill="none"'):
                self.assertNotIn(forbidden, source, f"{name}: {forbidden}")

    @unittest.skipUnless(svg_loader_available(), "no SVG pixbuf loader (librsvg)")
    def test_icons_render_as_distinct_glyphs_at_panel_sizes(self):
        from gi.repository import GdkPixbuf
        for size in (16, 22, 24):
            rendered = {}
            for name in ICON_NAMES:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(ICON_DIR / f"{name}.svg"), size, size)
                self.assertEqual((pixbuf.get_width(), pixbuf.get_height()), (size, size), name)
                count, _ = opaque_pixels(pixbuf)
                self.assertGreater(count, size * size * 0.15, f"{name} @ {size}: too sparse")
                self.assertLess(count, size * size * 0.85, f"{name} @ {size}: too dense")
                rendered[name] = bytes(pixbuf.get_pixels())
            self.assertEqual(len(set(rendered.values())), len(ICON_NAMES), f"identical glyphs at {size}")

    @unittest.skipUnless(gtk_available() and svg_loader_available(), "needs GTK and librsvg")
    def test_gtk_recolors_the_icons_as_symbolic(self):
        from tests.gtkbind import gtk_session
        gtk_session()
        from gi.repository import Gdk, Gtk
        theme = Gtk.IconTheme.get_default()
        theme.append_search_path(str(ICON_DIR))
        white = Gdk.RGBA(1, 1, 1, 1)
        for name in ICON_NAMES:
            info = theme.lookup_icon(name, 22, Gtk.IconLookupFlags.FORCE_SIZE)
            self.assertIsNotNone(info, name)
            self.assertTrue(info.is_symbolic(), name)
            pixbuf, was_symbolic = info.load_symbolic(white, None, None, None)
            self.assertTrue(was_symbolic, name)
            count, all_white = opaque_pixels(pixbuf)
            self.assertGreater(count, 0, name)
            self.assertTrue(all_white, f"{name} was not recolored to the panel foreground")


if __name__ == "__main__":
    unittest.main()

"""The Settings window on a real (Xvfb) display, built around stubs for every
collaborator: the switches and combos write through to the preferences (and only on
user edits — programmatic refreshes are guarded), conditional rows follow the
preferences, the hotkey recorder captures a real chord sent with ``xdotool`` and rejects
a bare key, the account section reflects the vault, and closing hides the window.

Chords are sent with ``xdotool key --window`` (XSendEvent to the toplevel), which GTK
accepts with the right modifier state — except Ctrl+Alt+F1…F12: those are the X server's
VT-switch chords, and under Xvfb's keymap the F-key press never reaches a client by any
route (XSendEvent, XTest, or ``Gtk.test_widget_send_key``), so Ctrl+Alt+F7 is delivered
as a synthesized ``Gdk.EventKey`` on the toplevel, which is exactly where the recorder
listens."""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session

from gans.entry import AuthEntry
from gans.hotkeyspec import HotkeySpec
from gans.prefs import DeliveryMode, Preferences

SECRET = "JBSWY3DPEHPK3PXP"


def make_entries():
    uris = [
        ("gh", f"otpauth://totp/GitHub:alice@example.com?secret={SECRET}&issuer=GitHub"),
        ("gg", f"otpauth://totp/Google:alice@gmail.com?secret={SECRET}&issuer=Google"),
        ("aws", f"otpauth://totp/AWS:root?secret={SECRET}&issuer=AWS"),
    ]
    entries = [AuthEntry.parse(uri, entry_id) for entry_id, uri in uris]
    assert all(entries)
    return entries


# MARK: Stubs

class FakeVault:
    def __init__(self):
        self.is_signed_in = True
        self.account_email = "alice@example.com"
        self.entries = make_entries()
        self.last_sync = 1_700_000_000.0
        self.keyring_persistent = True
        self.sign_outs = 0
        self._observers = []

    def on_change(self, callback):
        self._observers.append(callback)

    def notify(self):
        for callback in list(self._observers):
            callback()

    def sign_out(self):
        self.sign_outs += 1
        self.is_signed_in = False
        self.account_email = None
        self.entries = []
        self.last_sync = None
        self.notify()


class FakeStatus:
    def __init__(self, backend, ok, detail):
        self.backend = backend
        self.ok = ok
        self.detail = detail


class FakeHotkeyManager:
    def __init__(self):
        self.status = FakeStatus("x11", True, "Ctrl+Alt+Space is grabbed on the X server.")

    def manual_instructions(self):
        return "Bind gans toggle yourself."


class FakeAppLock:
    def __init__(self):
        self.is_enabled = False
        self.is_locked = False
        self.locks = 0

    def lock(self):
        self.locks += 1
        self.is_locked = True


class FakeInjector:
    def __init__(self, can_inject=True):
        self.can_inject = can_inject


class FakeLaunchAtLogin:
    def __init__(self):
        self.enabled = False
        self.sets = []

    def is_enabled(self):
        return self.enabled

    def set(self, enabled):
        self.sets.append(enabled)
        self.enabled = enabled


class FakeUpdateChecker:
    def __init__(self):
        self.automatic_checks_enabled = True
        self.last_check_date = None
        self.is_checking = False
        self.checks = 0
        self._observers = []

    def on_change(self, callback):
        self._observers.append(callback)

    def notify(self):
        for callback in list(self._observers):
            callback()

    def check_now(self):
        self.checks += 1


class FakeApp:
    def __init__(self):
        self.windows = []

    def add_window(self, window):
        self.windows.append(window)


@unittest.skipUnless(gtk_available() and shutil.which("xdotool"), "needs GTK and xdotool")
class SettingsUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()
        from gans.ui import settings
        cls.settings_module = settings

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefs = Preferences(Path(self.tmp.name) / "preferences.json")
        self.vault = FakeVault()
        self.hotkeys = FakeHotkeyManager()
        self.app_lock = FakeAppLock()
        self.injector = FakeInjector()
        self.launch = FakeLaunchAtLogin()
        self.checker = FakeUpdateChecker()
        self.app = FakeApp()
        self.events = []
        self.controller = self.settings_module.SettingsWindowController(
            self.prefs, self.vault, self.checker, self.hotkeys, self.app_lock, self.injector, app=self.app,
            launch_at_login=self.launch)
        self.controller.on_sign_in = lambda: self.events.append("sign_in")
        self.controller.on_hotkey_changed = self._hotkey_changed
        self.addCleanup(self._dispose)

    def _hotkey_changed(self):
        self.events.append("hotkey")
        # The app re-registers here; the recorder's caption must pick up the new status.
        self.hotkeys.status = FakeStatus("x11", True, f"{self.prefs.hotkey.display_string} is grabbed on the X server.")

    def _dispose(self):
        if self.controller.window is not None:
            self.controller.window.destroy()
        pump(50)

    # MARK: Helpers

    def show(self):
        self.controller.show()
        self.assertTrue(wait_until(lambda: self.controller.window is not None and self.controller.window.get_mapped()))
        pump(100)
        return self.controller.window

    def key(self, chord):
        xid = self.controller.window.get_window().get_xid()
        subprocess.run(["xdotool", "key", "--window", str(xid), chord], check=True, timeout=10)
        pump(150)

    # MARK: Window

    def test_builds_with_stubs_and_shows_every_section(self):
        window = self.show()
        self.assertEqual(window.get_title(), "Gans Settings")
        self.assertEqual(self.app.windows, [window])
        self.assertGreaterEqual(window.get_allocated_width(), self.settings_module.WINDOW_WIDTH)
        self.assertGreaterEqual(window.get_allocated_height(), self.settings_module.MIN_HEIGHT)
        sections = [child for child in window.get_child().get_child().get_child().get_children()]
        titles = [section.get_children()[0].get_text() for section in sections]
        self.assertEqual(titles, ["Account", "Most used", "Quick Search", "Security", "Typing", "Startup", "Updates"])
        for section in sections:
            self.assertTrue(section.get_style_context().has_class("gans-settings-section"))
            for row in section.get_children()[1].get_child().get_children():
                self.assertTrue(row.get_style_context().has_class("gans-settings-row"))

    def test_show_creates_once_and_delete_event_hides(self):
        window = self.show()
        window.close()  # the WM close button → delete-event
        pump(50)
        self.assertFalse(window.get_visible())
        self.controller.show()
        self.assertIs(self.controller.window, window)
        self.assertTrue(wait_until(window.get_visible))
        self.controller.close()
        self.assertFalse(window.get_visible())

    # MARK: Account

    def test_account_section_when_signed_in(self):
        window = self.show()
        self.assertTrue(window.signed_in_row.get_visible())
        self.assertEqual(window.signed_in_label.get_text(), "alice@example.com")
        self.assertTrue(window.last_sync_row.get_visible())
        self.assertEqual(window.last_sync_label.get_text(),
                         time.strftime("%b %-d, %Y, %H:%M", time.localtime(1_700_000_000)))
        self.assertEqual(window.entries_label.get_text(), "3")
        self.assertTrue(window.sign_out_row.get_visible())
        self.assertFalse(window.sign_in_row.get_visible())
        self.assertFalse(window.keyring_warning_row.get_visible())
        self.assertTrue(window.sign_out_button.get_style_context().has_class("destructive-action"))

    def test_sign_out_calls_the_vault_and_the_rows_follow(self):
        window = self.show()
        window.sign_out_button.clicked()
        pump(50)
        self.assertEqual(self.vault.sign_outs, 1)
        self.assertFalse(window.signed_in_row.get_visible())
        self.assertFalse(window.sign_out_row.get_visible())
        self.assertTrue(window.sign_in_row.get_visible())
        self.assertFalse(window.most_used_section.get_visible())
        window.sign_in_button.clicked()
        self.assertEqual(self.events, ["sign_in"])

    def test_keyring_warning_when_the_session_is_memory_only(self):
        self.vault.keyring_persistent = False
        window = self.show()
        self.assertTrue(window.keyring_warning_row.get_visible())
        self.assertEqual(window.keyring_warning_label.get_text(), self.settings_module.NO_KEYRING_WARNING)

    def test_vault_changes_refresh_in_place(self):
        window = self.show()
        self.vault.entries = self.vault.entries[:1]
        self.vault.last_sync = None
        self.vault.notify()
        pump(50)
        self.assertEqual(window.entries_label.get_text(), "1")
        self.assertFalse(window.last_sync_row.get_visible())

    # MARK: Most used

    def test_most_used_lists_the_top_entries_and_hides_when_empty(self):
        window = self.show()
        self.assertFalse(window.most_used_section.get_visible())
        for _ in range(3):
            self.prefs.record_usage("gh")
        self.prefs.record_usage("aws")
        self.prefs.record_usage("gone")  # an entry that no longer exists is skipped
        pump(50)
        self.assertTrue(window.most_used_section.get_visible())
        self.assertEqual(window.most_used_texts, [("GitHub (alice@example.com)", "3×"), ("AWS (root)", "1×")])

    # MARK: Quick Search

    def test_show_codes_switch_updates_prefs(self):
        window = self.show()
        self.assertFalse(window.show_codes_switch.get_active())
        window.show_codes_switch.set_active(True)
        pump(20)
        self.assertTrue(self.prefs.show_codes_in_quick_search)
        window.show_codes_switch.set_active(False)
        pump(20)
        self.assertFalse(self.prefs.show_codes_in_quick_search)

    def test_delivery_mode_hides_and_shows_the_also_copy_row(self):
        window = self.show()
        self.assertEqual(window.delivery_combo.get_active_id(), "type")
        self.assertTrue(window.also_copy_row.get_visible())
        window.delivery_combo.set_active_id("paste")
        pump(20)
        self.assertIs(self.prefs.delivery_mode, DeliveryMode.PASTE)
        self.assertFalse(window.also_copy_row.get_visible())
        window.delivery_combo.set_active_id("type")
        pump(20)
        self.assertIs(self.prefs.delivery_mode, DeliveryMode.TYPE)
        self.assertTrue(window.also_copy_row.get_visible())

    def test_clear_clipboard_switch_and_delay(self):
        window = self.show()
        self.assertTrue(window.clear_after_row.get_visible())
        self.assertEqual(window.clear_after_combo.get_active_id(), "30")
        window.clear_after_combo.set_active_id("120")
        pump(20)
        self.assertEqual(self.prefs.clear_clipboard_seconds, 120)
        window.clear_clipboard_switch.set_active(False)
        pump(20)
        self.assertFalse(self.prefs.clear_clipboard_enabled)
        self.assertFalse(window.clear_after_row.get_visible())
        self.assertIsNone(self.prefs.clipboard_clear_delay)

    def test_other_switches_write_through(self):
        window = self.show()
        window.also_copy_switch.set_active(False)
        window.honk_switch.set_active(True)
        window.require_unlock_switch.set_active(True)
        pump(20)
        self.assertFalse(self.prefs.also_copy_when_typing)
        self.assertTrue(self.prefs.honk_on_copy)
        self.assertTrue(self.prefs.require_unlock)

    def test_external_pref_changes_refresh_without_feedback(self):
        window = self.show()
        saves = []
        original_save = self.prefs.save
        self.prefs.save = lambda: (saves.append(1), original_save())
        self.prefs.honk_on_copy = True
        pump(20)
        self.assertTrue(window.honk_switch.get_active())
        # Exactly one save: the refresh didn't write the preferences back.
        self.assertEqual(len(saves), 1)

    # MARK: Hotkey recorder

    def synthesize_key(self, keyval, state):
        """Emits ``key-press-event`` on the toplevel with a fully formed ``Gdk.EventKey``
        (real hardware keycode from the keymap, so layout translation runs too)."""
        Gdk = self.settings_module.Gdk
        window = self.controller.window
        found, keys = Gdk.Keymap.get_for_display(window.get_display()).get_entries_for_keyval(keyval)
        self.assertTrue(found)
        event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
        event.window = window.get_window()
        event.time = Gdk.CURRENT_TIME
        event.keyval = keyval
        event.state = state
        event.hardware_keycode = keys[0].keycode
        event.group = keys[0].group
        handled = window.emit("key-press-event", event)
        pump(50)
        return handled

    def test_hotkey_recorder_captures_ctrl_alt_f7(self):
        Gdk = self.settings_module.Gdk
        window = self.show()
        recorder = window.hotkey_recorder
        self.assertEqual(recorder.get_label(), "Ctrl+Alt+Space")
        self.assertEqual(window.hotkey_status_label.get_text(), "Ctrl+Alt+Space is grabbed on the X server.")
        recorder.clicked()
        pump(50)
        self.assertTrue(recorder.is_recording)
        self.assertEqual(recorder.get_label(), "Press keys…")
        chord = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK
        self.assertTrue(self.synthesize_key(Gdk.KEY_F7, chord))
        self.assertFalse(recorder.is_recording)
        self.assertEqual(self.prefs.hotkey, HotkeySpec(key="F7", control=True, alt=True))
        self.assertEqual(recorder.get_label(), "Ctrl+Alt+F7")
        self.assertEqual(self.events, ["hotkey"])
        self.assertEqual(window.hotkey_status_label.get_text(), "Ctrl+Alt+F7 is grabbed on the X server.")
        # Once recorded, the toplevel handler is gone: the same chord is ordinary input.
        self.synthesize_key(Gdk.KEY_F7, chord)
        self.assertEqual(self.events, ["hotkey"])

    def test_hotkey_recorder_captures_a_real_chord_ahead_of_window_accelerators(self):
        Gdk, Gtk = self.settings_module.Gdk, self.settings_module.Gtk
        window = self.show()
        recorder = window.hotkey_recorder
        # A window accelerator on the same chord must not fire while recording: the
        # recorder listens on the toplevel ahead of GTK's accelerator handling.
        fired = []
        accel_group = Gtk.AccelGroup()
        window.add_accel_group(accel_group)
        accel_group.connect(Gdk.KEY_g, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK,
                            Gtk.AccelFlags.VISIBLE, lambda *_args: fired.append(1) or True)
        recorder.clicked()
        pump(50)
        self.key("ctrl+alt+g")
        self.assertFalse(recorder.is_recording)
        self.assertEqual(self.prefs.hotkey, HotkeySpec(key="g", control=True, alt=True))
        self.assertEqual(recorder.get_label(), "Ctrl+Alt+G")
        self.assertEqual(self.events, ["hotkey"])
        self.assertEqual(window.hotkey_status_label.get_text(), "Ctrl+Alt+G is grabbed on the X server.")
        self.assertEqual(fired, [])
        # Not recording any more: the same chord now reaches the accelerator as usual.
        self.key("ctrl+alt+g")
        self.assertEqual(fired, [1])
        self.assertEqual(self.events, ["hotkey"])

    def test_hotkey_recorder_rejects_a_chord_without_modifiers(self):
        window = self.show()
        recorder = window.hotkey_recorder
        recorder.clicked()
        pump(50)
        self.key("F7")
        self.assertTrue(recorder.is_recording)  # beeped and kept waiting
        self.assertEqual(recorder.get_label(), "Press keys…")
        self.assertEqual(self.prefs.hotkey, HotkeySpec.DEFAULT)
        self.assertEqual(self.events, [])
        self.key("ctrl")  # a bare modifier press is ignored too
        self.assertTrue(recorder.is_recording)
        self.key("Escape")
        self.assertFalse(recorder.is_recording)
        self.assertEqual(recorder.get_label(), "Ctrl+Alt+Space")
        self.assertEqual(self.prefs.hotkey, HotkeySpec.DEFAULT)

    def test_hotkey_recorder_uses_the_unshifted_key_and_super(self):
        window = self.show()
        window.hotkey_recorder.clicked()
        pump(50)
        self.key("super+shift+1")
        self.assertEqual(self.prefs.hotkey, HotkeySpec(key="1", shift=True, super_=True))
        self.assertEqual(window.hotkey_recorder.get_label(), "Shift+Super+1")

    def test_hotkey_status_falls_back_to_manual_instructions(self):
        self.hotkeys.status = FakeStatus("none", False, "")
        window = self.show()
        self.assertEqual(window.hotkey_status_label.get_text(), "Bind gans toggle yourself.")

    # MARK: Security / typing / startup / updates

    def test_lock_now(self):
        window = self.show()
        window.lock_now_button.clicked()
        self.assertEqual(self.app_lock.locks, 1)

    def test_typing_status(self):
        window = self.show()
        self.assertEqual(window.typing_label.get_text(), self.settings_module.TYPING_AVAILABLE)
        self.injector.can_inject = False
        window.refresh()
        self.assertEqual(window.typing_label.get_text(), self.settings_module.TYPING_UNAVAILABLE)
        self.assertEqual(window.typing_icon.get_icon_name()[0], "dialog-warning-symbolic")

    def test_launch_at_login_switch(self):
        window = self.show()
        self.assertFalse(window.launch_at_login_switch.get_active())
        window.launch_at_login_switch.set_active(True)
        pump(20)
        self.assertEqual(self.launch.sets, [True])
        window.launch_at_login_switch.set_active(False)
        pump(20)
        self.assertEqual(self.launch.sets, [True, False])

    def test_updates_section(self):
        window = self.show()
        self.assertTrue(window.auto_update_switch.get_active())
        self.assertFalse(window.last_checked_row.get_visible())
        self.assertTrue(window.check_now_button.get_sensitive())
        window.auto_update_switch.set_active(False)
        pump(20)
        self.assertFalse(self.checker.automatic_checks_enabled)
        window.check_now_button.clicked()
        self.assertEqual(self.checker.checks, 1)
        self.checker.is_checking = True
        self.checker.last_check_date = 1_700_000_000.0
        self.checker.notify()
        pump(20)
        self.assertFalse(window.check_now_button.get_sensitive())
        self.assertTrue(window.last_checked_row.get_visible())
        self.assertEqual(window.last_checked_label.get_text(),
                         time.strftime("%b %-d, %Y, %H:%M", time.localtime(1_700_000_000)))


if __name__ == "__main__":
    unittest.main()

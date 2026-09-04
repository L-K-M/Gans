import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session, present_now

from gans.hotkeyspec import HotkeySpec
from gans.platform.x11 import X11HotkeyGrabber, X11Session


class InertX11Tests(unittest.TestCase):
    """Without a display every method degrades to None/False and never raises."""

    def assert_inert(self, x11):
        self.assertFalse(x11.available)
        self.assertFalse(x11.has_xtest)
        self.assertIsNone(x11.active_window())
        x11.activate_window(0x400001)
        self.assertIsNone(x11.window_name(0x400001))
        self.assertFalse(x11.type_text("123456"))
        self.assertFalse(x11.send_ctrl_v())
        x11.close()

    def test_no_display_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            x11 = X11Session()
            self.assertIsNone(x11.display_name)
            self.assert_inert(x11)

    def test_empty_display_variable(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ""}, clear=True):
            self.assert_inert(X11Session())

    def test_unreachable_display(self):
        self.assert_inert(X11Session(":59000"))

    def test_hotkey_grabber_without_display(self):
        grabber = X11HotkeyGrabber(X11Session(":59000"), dispatch=lambda fn: fn())
        self.assertFalse(grabber.register(HotkeySpec.DEFAULT, lambda: None))
        grabber.unregister()


@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class X11SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()
        cls.x11 = X11Session()

    @classmethod
    def tearDownClass(cls):
        cls.x11.close()

    def setUp(self):
        from gi.repository import Gtk
        self.window = Gtk.Window(title="Gans target")
        self.entry = Gtk.Entry()
        self.window.add(self.entry)
        self.entry.grab_focus()
        present_now(self.window)
        pump(150)

    def tearDown(self):
        self.window.destroy()
        pump(50)

    def type_in_background(self, text, expected):
        """Types from a worker thread while the GTK loop runs, as a real target app would
        be running — the scratch-keycode path needs the target to translate the key
        before the mapping is restored."""
        results = []
        worker = threading.Thread(target=lambda: results.append(self.x11.type_text(text)))
        worker.start()
        arrived = wait_until(lambda: self.entry.get_text() == expected, timeout=5)
        worker.join(timeout=5)
        self.assertEqual(results, [True])
        self.assertTrue(arrived, f"entry has {self.entry.get_text()!r}")

    def test_capabilities(self):
        self.assertTrue(self.x11.available)
        self.assertTrue(self.x11.has_xtest)
        self.assertEqual(self.x11.display_name, self.session.display_name)

    def test_type_digits(self):
        self.assertTrue(self.x11.type_text("123456"))
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "123456"))

    def test_type_steam_code_uses_shift(self):
        self.assertTrue(self.x11.type_text("N7QKX"))
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "N7QKX"))

    def test_type_symbols_absent_from_layout_via_remap(self):
        self.type_in_background("é1ü€Ω", "é1ü€Ω")

    def test_type_altgr_symbols_on_german_layout(self):
        if shutil.which("setxkbmap") is None:
            self.skipTest("setxkbmap not installed")
        subprocess.run(["setxkbmap", "-layout", "de"], check=True)
        try:
            pump(100)
            # ü is a plain key, Ü needs Shift, @ and { live on AltGr — and the mapping is
            # read live, so the switch made after connecting is honoured.
            self.assertTrue(self.x11.type_text("ü@{Ü"))
            self.assertTrue(wait_until(lambda: self.entry.get_text() == "ü@{Ü"), self.entry.get_text())
        finally:
            subprocess.run(["setxkbmap", "-layout", "us"], check=True)
            pump(100)

    def test_type_with_caps_lock_on_restores_it(self):
        from Xlib import X, XK
        import Xlib.display
        probe = Xlib.display.Display()
        try:
            root = probe.screen().root
            subprocess.run(["xdotool", "key", "Caps_Lock"], check=True)
            self.assertTrue(wait_until(lambda: probe.sync() or root.query_pointer().mask & X.LockMask))
            try:
                self.assertTrue(self.x11.type_text("N7q"))
                self.assertTrue(wait_until(lambda: self.entry.get_text() == "N7q"), self.entry.get_text())
                self.assertTrue(root.query_pointer().mask & X.LockMask, "Caps Lock should be restored")
            finally:
                subprocess.run(["xdotool", "key", "Caps_Lock"], check=True)
                wait_until(lambda: probe.sync() or not root.query_pointer().mask & X.LockMask)
        finally:
            probe.close()

    def test_send_ctrl_v_pastes_clipboard(self):
        from gans.platform.clipboard import Clipboard
        clipboard = Clipboard()
        try:
            self.assertTrue(clipboard.copy("987654"))
            self.assertTrue(self.x11.send_ctrl_v())
            self.assertTrue(wait_until(lambda: self.entry.get_text() == "987654"))
        finally:
            clipboard.release()

    def test_active_window_is_the_presented_window(self):
        self.assertEqual(self.x11.active_window(), self.window.get_window().get_xid())

    def test_activate_window_round_trips(self):
        from gi.repository import Gtk
        other = Gtk.Window(title="Other")
        other.add(Gtk.Entry())
        present_now(other)
        pump(150)
        try:
            first, second = self.window.get_window().get_xid(), other.get_window().get_xid()
            self.assertEqual(self.x11.active_window(), second)
            self.x11.activate_window(first)
            self.assertTrue(wait_until(lambda: self.x11.active_window() == first))
            self.assertTrue(wait_until(self.window.is_active))  # GTK saw the FocusIn too
            self.x11.activate_window(second)
            self.assertTrue(wait_until(lambda: self.x11.active_window() == second))
            self.assertTrue(wait_until(other.is_active))
        finally:
            other.destroy()
            pump(50)

    def test_activate_window_tolerates_a_vanished_window(self):
        from gi.repository import Gtk
        doomed = Gtk.Window(title="Doomed")
        doomed.show_all()
        pump(50)
        xid = doomed.get_window().get_xid()
        doomed.destroy()
        pump(50)
        self.x11.activate_window(xid)  # must not raise
        self.assertIsNone(self.x11.window_name(xid))

    def test_window_name(self):
        name = self.x11.window_name(self.window.get_window().get_xid())
        self.assertTrue(name)
        self.assertEqual(name[0], name[0].upper())

    def test_window_name_falls_back_to_the_title(self):
        from gi.repository import Gtk
        from Xlib import Xatom
        import Xlib.display
        plain = Gtk.Window(title="Titled only")
        plain.show_all()
        pump(50)
        xid = plain.get_window().get_xid()
        probe = Xlib.display.Display()
        try:
            probe.create_resource_object("window", xid).delete_property(Xatom.WM_CLASS)
            probe.sync()
            self.assertEqual(self.x11.window_name(xid), "Titled only")
        finally:
            probe.close()
            plain.destroy()
            pump(50)


@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class X11HotkeyGrabberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("xdotool") is None:
            raise unittest.SkipTest("xdotool not installed")
        cls.session = gtk_session()
        cls.x11 = X11Session()

    @classmethod
    def tearDownClass(cls):
        cls.x11.close()

    def setUp(self):
        self.fired = threading.Event()
        self.grabber = X11HotkeyGrabber(self.x11, dispatch=lambda fn: fn())

    def tearDown(self):
        self.grabber.unregister()

    @staticmethod
    def press(combination):
        subprocess.run(["xdotool", "key", combination], check=True)

    def assert_fires(self, combination):
        self.fired.clear()
        self.press(combination)
        self.assertTrue(wait_until(self.fired.is_set, timeout=3), f"{combination} did not fire")

    def assert_silent(self, combination):
        self.fired.clear()
        self.press(combination)
        pump(400)
        self.assertFalse(self.fired.is_set(), f"{combination} fired unexpectedly")

    def test_default_hotkey_fires_and_stops_after_unregister(self):
        self.assertTrue(self.grabber.register(HotkeySpec.DEFAULT, self.fired.set))
        self.assert_fires("ctrl+alt+space")
        self.assert_silent("ctrl+space")
        self.grabber.unregister()
        self.assert_silent("ctrl+alt+space")

    def test_fires_with_num_lock_on(self):
        self.assertTrue(self.grabber.register(HotkeySpec.DEFAULT, self.fired.set))
        self.press("Num_Lock")
        try:
            self.assert_fires("ctrl+alt+space")
        finally:
            self.press("Num_Lock")

    def test_reregister_replaces_the_grab(self):
        self.assertTrue(self.grabber.register(HotkeySpec.DEFAULT, self.fired.set))
        # (Not Ctrl+Alt+F9: Ctrl+Alt+F1…F12 are XKB's VT-switch chords, which the server
        # consumes before any grab can see them.)
        self.assertTrue(self.grabber.register(HotkeySpec(key="F9", control=True, shift=True), self.fired.set))
        self.assert_fires("ctrl+shift+F9")
        self.assert_silent("ctrl+alt+space")

    def test_conflicting_grab_is_reported(self):
        self.assertTrue(self.grabber.register(HotkeySpec.DEFAULT, self.fired.set))
        rival = X11HotkeyGrabber(self.x11, dispatch=lambda fn: fn())
        try:
            self.assertFalse(rival.register(HotkeySpec.DEFAULT, lambda: None))
        finally:
            rival.unregister()
        self.assert_fires("ctrl+alt+space")  # the original grab is intact

    def test_unknown_key_is_rejected(self):
        self.assertFalse(self.grabber.register(HotkeySpec(key="NoSuchKeysym", control=True), self.fired.set))

    def test_dispatch_receives_the_callback(self):
        dispatched = []
        grabber = X11HotkeyGrabber(self.x11, dispatch=dispatched.append)
        try:
            self.assertTrue(grabber.register(HotkeySpec(key="F8", super_=True), self.fired.set))
            self.press("super+F8")
            self.assertTrue(wait_until(lambda: len(dispatched) == 1, timeout=3))
            self.assertEqual(dispatched[0], self.fired.set)
            self.assertFalse(self.fired.is_set())  # only dispatch may run it
        finally:
            grabber.unregister()


if __name__ == "__main__":
    unittest.main()

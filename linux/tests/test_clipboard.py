import os
import subprocess
import sys
import unittest

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session

from gans.platform.clipboard import PASSWORD_HINT_TARGET, Clipboard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClipboardWithoutDisplayTests(unittest.TestCase):
    def test_copy_reports_failure_without_a_display(self):
        env = {key: value for key, value in os.environ.items() if key not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env["NO_AT_BRIDGE"] = "1"
        script = ("import sys; sys.path.insert(0, sys.argv[1]); "
                  "from gans.platform.clipboard import Clipboard; "
                  "c = Clipboard(); print(c.copy('123456', clear_after=5), c.clear_if_still('123456'))")
        completed = subprocess.run([sys.executable, "-c", script, ROOT], env=env, capture_output=True, text=True,
                                   timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "False False")


@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class ClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()

    def setUp(self):
        from gi.repository import Gdk, Gtk
        self.gtk_clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.gtk_clipboard.clear()
        pump(20)
        self.clipboard = Clipboard()

    def tearDown(self):
        self.clipboard.release()
        pump(20)

    def text(self):
        return self.gtk_clipboard.wait_for_text()

    def test_copy_serves_text_and_the_password_manager_hint(self):
        from gi.repository import Gdk
        self.assertTrue(self.clipboard.copy("123456"))
        self.assertEqual(self.text(), "123456")
        found, targets = self.gtk_clipboard.wait_for_targets()
        names = [atom.name() for atom in targets]
        for name in ("UTF8_STRING", "TEXT", "STRING", "text/plain;charset=utf-8", "text/plain", PASSWORD_HINT_TARGET):
            self.assertIn(name, names)
        hint = self.gtk_clipboard.wait_for_contents(Gdk.Atom.intern(PASSWORD_HINT_TARGET, False))
        self.assertEqual(bytes(hint.get_data()), b"secret")

    def test_copy_replaces_a_previous_copy(self):
        self.clipboard.copy("111111")
        self.clipboard.copy("222222")
        self.assertEqual(self.text(), "222222")
        self.assertFalse(self.clipboard.clear_if_still("111111"))
        self.assertEqual(self.text(), "222222")

    def test_clear_if_still_clears_only_our_matching_text(self):
        self.clipboard.copy("123456")
        self.assertFalse(self.clipboard.clear_if_still("654321"))
        self.assertEqual(self.text(), "123456")
        self.assertTrue(self.clipboard.clear_if_still("123456"))
        pump(50)
        self.assertIsNone(self.text())
        self.assertFalse(self.clipboard.clear_if_still("123456"))

    def test_clear_after_wipes_the_code_if_still_ours(self):
        self.assertTrue(self.clipboard.copy("123456", clear_after=1))
        self.assertEqual(self.text(), "123456")
        self.assertTrue(wait_until(lambda: self.text() is None, timeout=4))

    def test_clear_after_leaves_a_newer_foreign_copy_alone(self):
        self.clipboard.copy("123456", clear_after=0.5)
        self.gtk_clipboard.set_text("something the user copied", -1)
        pump(50)
        self.assertFalse(self.clipboard.clear_if_still("123456"))  # no longer ours
        pump(1000)
        self.assertEqual(self.text(), "something the user copied")

    def test_clear_after_leaves_a_newer_own_copy_alone(self):
        self.clipboard.copy("111111", clear_after=0.5)
        self.clipboard.copy("222222")
        pump(1000)
        self.assertEqual(self.text(), "222222")

    def test_foreign_owner_from_another_client_is_detected(self):
        from Xlib import X
        import Xlib.display
        self.clipboard.copy("123456")
        other = Xlib.display.Display()
        try:
            window = other.screen().root.create_window(0, 0, 1, 1, 0, other.screen().root_depth)
            window.set_selection_owner(other.intern_atom("CLIPBOARD"), X.CurrentTime)
            other.sync()
            self.assertTrue(wait_until(lambda: not self.clipboard.clear_if_still("123456")))
            self.assertEqual(other.get_selection_owner(other.intern_atom("CLIPBOARD")).id, window.id)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()

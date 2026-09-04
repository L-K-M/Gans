import time
import unittest

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session, present_now

from gans.platform.inject import CodeInjector, DeliveryResult
from gans.prefs import DeliveryMode


class FakeX11:
    def __init__(self, available=False, has_xtest=False, typing_works=True, pasting_works=True):
        self.available = available
        self.has_xtest = has_xtest
        self.typing_works = typing_works
        self.pasting_works = pasting_works
        self.activated = []
        self.typed = []
        self.pastes = 0

    def activate_window(self, window_id):
        self.activated.append(window_id)

    def type_text(self, text):
        self.typed.append(text)
        return self.typing_works

    def send_ctrl_v(self):
        self.pastes += 1
        return self.pasting_works


class FakeClipboard:
    def __init__(self):
        self.copies = []

    def copy(self, text, clear_after=None):
        self.copies.append((text, clear_after))
        return True


def pump_glib(seconds):
    """Runs the GLib main context (no GTK/display needed) for the injector's timer."""
    from gi.repository import GLib
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.005)


class CodeInjectorLogicTests(unittest.TestCase):
    def setUp(self):
        self.clipboard = FakeClipboard()
        self.results = []

    def test_without_x11_the_code_is_copied_only(self):
        x11 = FakeX11(available=False)
        injector = CodeInjector(self.clipboard, x11)
        self.assertFalse(injector.can_inject)
        result = injector.deliver("123456", 0x1234, DeliveryMode.TYPE, also_copy=False,
                                  clear_clipboard_after=30, completion=self.results.append)
        self.assertIs(result, DeliveryResult.COPIED_ONLY)
        self.assertEqual(self.results, [DeliveryResult.COPIED_ONLY])
        self.assertEqual(self.clipboard.copies, [("123456", 30)])
        self.assertEqual(x11.activated, [])
        self.assertEqual(x11.typed, [])

    def test_xtest_is_required(self):
        self.assertFalse(CodeInjector(self.clipboard, FakeX11(available=True, has_xtest=False)).can_inject)
        self.assertTrue(CodeInjector(self.clipboard, FakeX11(available=True, has_xtest=True)).can_inject)

    def test_type_mode_activates_then_types_after_the_delay(self):
        x11 = FakeX11(available=True, has_xtest=True)
        injector = CodeInjector(self.clipboard, x11)
        started = time.monotonic()
        result = injector.deliver("123456", 0x1234, DeliveryMode.TYPE, also_copy=False, completion=self.results.append)
        self.assertIs(result, DeliveryResult.DELIVERED)
        self.assertEqual(x11.activated, [0x1234])
        self.assertEqual(x11.typed, [])  # deferred so the target can come to the front
        self.assertEqual(self.clipboard.copies, [])
        pump_glib(0.3)
        self.assertEqual(x11.typed, ["123456"])
        self.assertEqual(self.results, [DeliveryResult.DELIVERED])
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual(x11.pastes, 0)

    def test_also_copy_puts_the_code_on_the_clipboard_too(self):
        x11 = FakeX11(available=True, has_xtest=True)
        CodeInjector(self.clipboard, x11).deliver("123456", None, DeliveryMode.TYPE, also_copy=True,
                                                  clear_clipboard_after=15)
        self.assertEqual(self.clipboard.copies, [("123456", 15)])
        self.assertEqual(x11.activated, [])  # no target window: nothing to activate
        pump_glib(0.3)
        self.assertEqual(x11.typed, ["123456"])

    def test_paste_mode_copies_then_sends_ctrl_v(self):
        x11 = FakeX11(available=True, has_xtest=True)
        CodeInjector(self.clipboard, x11).deliver("123456", 0x1234, DeliveryMode.PASTE, also_copy=False,
                                                  completion=self.results.append)
        self.assertEqual(self.clipboard.copies, [("123456", None)])
        pump_glib(0.3)
        self.assertEqual(x11.pastes, 1)
        self.assertEqual(x11.typed, [])
        self.assertEqual(self.results, [DeliveryResult.DELIVERED])

    def test_typing_failure_falls_back_to_the_clipboard(self):
        x11 = FakeX11(available=True, has_xtest=True, typing_works=False)
        result = CodeInjector(self.clipboard, x11).deliver("123456", 0x1234, DeliveryMode.TYPE, also_copy=False,
                                                           clear_clipboard_after=30, completion=self.results.append)
        self.assertIs(result, DeliveryResult.DELIVERED)  # the optimistic answer
        pump_glib(0.3)
        self.assertEqual(self.results, [DeliveryResult.COPIED_ONLY])
        self.assertEqual(self.clipboard.copies, [("123456", 30)])

    def test_paste_failure_does_not_copy_twice(self):
        x11 = FakeX11(available=True, has_xtest=True, pasting_works=False)
        CodeInjector(self.clipboard, x11).deliver("123456", None, DeliveryMode.PASTE, also_copy=False,
                                                  completion=self.results.append)
        pump_glib(0.3)
        self.assertEqual(self.results, [DeliveryResult.COPIED_ONLY])
        self.assertEqual(len(self.clipboard.copies), 1)

    def test_paste_mode_types_when_the_clipboard_is_unavailable(self):
        class BrokenClipboard(FakeClipboard):
            def copy(self, text, clear_after=None):
                super().copy(text, clear_after)
                return False

        x11 = FakeX11(available=True, has_xtest=True)
        CodeInjector(BrokenClipboard(), x11).deliver("123456", None, DeliveryMode.PASTE, also_copy=False,
                                                     completion=self.results.append)
        pump_glib(0.3)
        self.assertEqual(x11.pastes, 0)
        self.assertEqual(x11.typed, ["123456"])
        self.assertEqual(self.results, [DeliveryResult.DELIVERED])


@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class CodeInjectorXvfbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gans.platform.clipboard import Clipboard
        from gans.platform.x11 import X11Session
        cls.session = gtk_session()
        cls.x11 = X11Session()
        cls.clipboard = Clipboard()
        cls.injector = CodeInjector(cls.clipboard, cls.x11)

    @classmethod
    def tearDownClass(cls):
        cls.clipboard.release()
        cls.x11.close()

    def setUp(self):
        from gi.repository import Gtk
        self.window = Gtk.Window(title="Target app")
        self.entry = Gtk.Entry()
        self.window.add(self.entry)
        self.entry.grab_focus()
        present_now(self.window)
        pump(150)
        self.target = self.x11.active_window()
        self.assertEqual(self.target, self.window.get_window().get_xid())
        self.results = []

    def tearDown(self):
        self.window.destroy()
        pump(50)

    def clipboard_text(self):
        from gi.repository import Gdk, Gtk
        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()

    def test_types_into_the_target(self):
        from gi.repository import Gdk, Gtk
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("whatever was there", -1)
        self.assertTrue(self.injector.can_inject)
        result = self.injector.deliver("123456", self.target, DeliveryMode.TYPE, also_copy=False,
                                       completion=self.results.append)
        self.assertIs(result, DeliveryResult.DELIVERED)
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "123456"))
        self.assertEqual(self.results, [DeliveryResult.DELIVERED])
        self.assertEqual(self.clipboard_text(), "whatever was there")  # typing leaves the clipboard alone

    def test_pastes_into_the_target(self):
        result = self.injector.deliver("654321", self.target, DeliveryMode.PASTE, also_copy=False,
                                       completion=self.results.append)
        self.assertIs(result, DeliveryResult.DELIVERED)
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "654321"))
        self.assertEqual(self.results, [DeliveryResult.DELIVERED])
        self.assertEqual(self.clipboard_text(), "654321")

    def test_clears_the_clipboard_after_the_delay(self):
        self.injector.deliver("111222", self.target, DeliveryMode.TYPE, also_copy=True, clear_clipboard_after=1)
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "111222"))
        self.assertEqual(self.clipboard_text(), "111222")
        self.assertTrue(wait_until(lambda: self.clipboard_text() is None, timeout=4))

    def test_does_not_clear_what_the_user_copied_meanwhile(self):
        from gi.repository import Gdk, Gtk
        self.injector.deliver("333444", self.target, DeliveryMode.PASTE, also_copy=False, clear_clipboard_after=0.5)
        self.assertTrue(wait_until(lambda: self.entry.get_text() == "333444"))
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("user's own text", -1)
        pump(1200)
        self.assertEqual(self.clipboard_text(), "user's own text")


if __name__ == "__main__":
    unittest.main()

"""The login window on a real (Xvfb) display: it renders each model stage into the right
stack page, the entries feed the model and gate the Sign In button, the buttons drive the
model, errors show in the error line, and closing hides the window rather than
destroying it. The model is real; the ``EnteLogin`` behind it is a scripted fake that
answers from a worker thread, exactly like the network one."""

import threading
import unittest

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session

from gans.ente.login import Authorized, LoginError, NeedsEmailCode, NeedsPasskey, NeedsTwoFactor
from gans.ente.models import AuthorizationResponse
from gans.ui.login_model import Stage

AUTH = AuthorizationResponse(id=1, encrypted_token="tok")


# MARK: Fakes

class FakeLogin:
    def __init__(self):
        self.calls = []
        self.srp = Authorized(AUTH)
        self.email_otp = NeedsEmailCode()
        self.verify = Authorized(AUTH)
        self.passkey_done = threading.Event()

    @staticmethod
    def _result(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def start_srp(self, email, password):
        self.calls.append(("srp", email, password))
        return self._result(self.srp)

    def send_email_otp(self, email):
        self.calls.append(("otp", email))
        return self._result(self.email_otp)

    def verify_email_otp(self, email, code):
        self.calls.append(("verify_email", email, code))
        return self._result(self.verify)

    def verify_two_factor(self, session_id, code):
        self.calls.append(("verify_2fa", session_id, code))
        return self._result(self.verify)

    def wait_for_passkey_token(self, session_id, timeout=180, poll_interval=2, is_cancelled=lambda: False):
        self.calls.append(("passkey", session_id))
        while not self.passkey_done.wait(0.01):
            if is_cancelled():
                raise InterruptedError("cancelled")
        return Authorized(AUTH)


class FakeVault:
    def __init__(self):
        self.logins = []

    def complete_login(self, authorization, password, email):
        self.logins.append((authorization, password, email))


class FakeApp:
    def __init__(self):
        self.presented = 0

    def present_after_login(self):
        self.presented += 1


@unittest.skipUnless(gtk_available(), "needs GTK")
class LoginUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()
        from gans.ui import login
        cls.login_module = login

    def setUp(self):
        self.login = FakeLogin()
        self.vault = FakeVault()
        self.app = FakeApp()
        self.controller = self.login_module.LoginWindowController(self.vault, api=None, app=self.app, login=self.login)
        self.addCleanup(self._dispose)

    def _dispose(self):
        if self.controller.window is not None:
            self.controller.window.destroy()
        pump(50)

    # MARK: Helpers

    def show(self):
        self.controller.show()
        self.assertTrue(wait_until(lambda: self.controller.window is not None and self.controller.window.get_mapped()))
        pump(50)
        return self.controller.window

    @property
    def model(self):
        return self.controller.model

    def fill_credentials(self, window, email="alice@example.com", password="hunter2"):
        window.email_entry.set_text(email)
        window.password_entry.set_text(password)
        pump(20)

    def wait_for_page(self, name):
        self.assertTrue(wait_until(lambda: self.controller.window.visible_page == name),
                        f"expected page {name!r}, got {self.controller.window.visible_page!r}")
        self.assertTrue(wait_until(lambda: not self.model.is_busy))
        pump(50)

    # MARK: Window

    def test_window_basics(self):
        window = self.show()
        self.assertEqual(window.get_title(), "Sign in to Ente")
        self.assertFalse(window.get_resizable())
        self.assertGreaterEqual(window.get_allocated_width(), self.login_module.CONTENT_WIDTH)
        self.assertEqual(window.visible_page, "credentials")
        self.assertIsNone(window.error_text)
        self.assertFalse(window.password_entry.get_visibility())
        self.assertIs(window.get_focus(), window.email_entry)

    def test_show_creates_the_window_once_and_close_hides_it(self):
        window = self.show()
        self.controller.close()
        self.assertFalse(window.get_visible())
        self.controller.show()
        self.assertIs(self.controller.window, window)
        self.assertTrue(wait_until(window.get_visible))

    def test_delete_event_hides_instead_of_destroying(self):
        window = self.show()
        self.fill_credentials(window)
        window.close()  # synthesizes the WM close (delete-event)
        pump(50)
        self.assertFalse(window.get_visible())
        self.assertIs(self.controller.window, window)
        # The typed state survives: the model (and its window) were kept.
        self.assertEqual(self.model.email, "alice@example.com")
        self.controller.show()
        pump(50)
        self.assertIs(window.get_focus(), window.password_entry)  # the caret lands where typing continues

    # MARK: Credentials page

    def test_typing_email_and_password_enables_sign_in(self):
        window = self.show()
        self.assertFalse(window.sign_in_button.get_sensitive())
        self.assertFalse(window.email_code_button.get_sensitive())
        window.email_entry.set_text("alice@example.com")
        pump(20)
        self.assertFalse(window.sign_in_button.get_sensitive())
        window.password_entry.set_text("hunter2")
        pump(20)
        self.assertTrue(window.sign_in_button.get_sensitive())
        self.assertTrue(window.email_code_button.get_sensitive())
        self.assertEqual(self.model.email, "alice@example.com")
        self.assertEqual(self.model.password, "hunter2")
        window.email_entry.set_text("   ")
        pump(20)
        self.assertFalse(window.sign_in_button.get_sensitive())

    def test_clicking_sign_in_calls_the_model_and_signs_in(self):
        window = self.show()
        self.fill_credentials(window, email=" alice@example.com ")
        window.sign_in_button.clicked()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))
        self.assertEqual(self.login.calls, [("srp", "alice@example.com", "hunter2")])
        self.assertEqual(self.vault.logins, [(AUTH, "hunter2", "alice@example.com")])
        self.assertFalse(window.get_visible())  # closed on success
        pump(50)
        self.assertEqual(window.password_entry.get_text(), "")  # the password is dropped
        self.assertEqual(self.model.password, "")

    def test_email_me_a_code_button(self):
        window = self.show()
        self.fill_credentials(window)
        window.email_code_button.clicked()
        self.wait_for_page("code")
        self.assertEqual(self.login.calls, [("otp", "alice@example.com")])
        self.assertEqual(window.code_title.get_text(), "Enter the code we emailed you")
        self.assertEqual(window.code_help.get_text(), "Check alice@example.com.")

    def test_busy_shows_a_spinner_and_disables_the_buttons(self):
        gate = threading.Event()

        def slow_srp(email, password):
            gate.wait()
            return Authorized(AUTH)
        self.login.start_srp = slow_srp
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        pump(50)
        self.assertTrue(self.model.is_busy)
        self.assertTrue(window.sign_in_button.is_spinning)
        self.assertFalse(window.sign_in_button.get_sensitive())
        self.assertFalse(window.email_code_button.get_sensitive())
        gate.set()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))
        self.assertFalse(window.sign_in_button.is_spinning)

    # MARK: Stage rendering

    def test_two_factor_stage_renders_the_code_page(self):
        self.login.srp = NeedsTwoFactor("sess-2fa")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("code")
        self.assertEqual(window.code_title.get_text(), "Two-factor code")
        self.assertEqual(window.code_help.get_text(), "Enter the 6-digit code from your authenticator.")
        self.assertFalse(window.continue_button.get_sensitive())  # no code yet
        window.code_entry.set_text("654321")
        pump(20)
        self.assertEqual(self.model.code, "654321")
        self.assertTrue(window.continue_button.get_sensitive())
        window.continue_button.clicked()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))
        self.assertEqual(self.login.calls[-1], ("verify_2fa", "sess-2fa", "654321"))

    def test_reshowing_after_a_two_factor_sign_in_starts_on_credentials(self):
        # Sign Out -> "Sign in to Ente..." re-presents the retained window; it must not
        # come back on the code page with the previous login's dead 2FA session.
        self.login.srp = NeedsTwoFactor("sess-2fa")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("code")
        window.code_entry.set_text("654321")
        pump(20)
        window.continue_button.clicked()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))
        self.assertTrue(wait_until(lambda: not window.get_visible()))
        self.controller.show()
        self.assertTrue(wait_until(window.get_visible))
        pump(50)
        self.assertEqual(window.visible_page, "credentials")
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertEqual(window.email_entry.get_text(), "alice@example.com")
        self.assertEqual(window.password_entry.get_text(), "")
        self.assertEqual(window.code_entry.get_text(), "")
        self.assertIsNone(window.error_text)
        self.assertIs(window.get_focus(), window.password_entry)

    def test_srp_unavailable_falls_back_to_the_email_code_page(self):
        self.login.srp = LoginError(LoginError.SRP_UNAVAILABLE)
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("code")
        self.assertEqual(window.code_title.get_text(), "Enter the code we emailed you")
        self.assertIsNone(window.error_text)

    def test_back_returns_to_credentials_and_keeps_the_email(self):
        self.login.srp = NeedsEmailCode()
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("code")
        window.code_entry.set_text("12")
        pump(20)
        window.back_button.clicked()
        pump(50)
        self.assertEqual(window.visible_page, "credentials")
        self.assertEqual(window.email_entry.get_text(), "alice@example.com")
        self.assertEqual(window.code_entry.get_text(), "")
        self.assertEqual(self.model.code, "")

    def test_passkey_stage_renders_opens_the_browser_and_waits(self):
        self.login.srp = NeedsPasskey("pk-sess", "https://accounts.ente.io")
        opened = []
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("passkey")
        self.assertEqual(window.passkey_cancel_button.get_label(), "Back")
        self.assertFalse(window.passkey_waiting_label.get_visible())

        original = self.login_module.Gtk.show_uri_on_window
        self.login_module.Gtk.show_uri_on_window = lambda parent, uri, timestamp: opened.append((parent, uri))
        try:
            window.passkey_button.clicked()
            pump(50)
        finally:
            self.login_module.Gtk.show_uri_on_window = original
        self.assertEqual(len(opened), 1)
        self.assertIs(opened[0][0], window)
        self.assertIn("passkeySessionID=pk-sess", opened[0][1])
        self.assertTrue(wait_until(lambda: ("passkey", "pk-sess") in self.login.calls))
        self.assertTrue(self.model.is_busy)
        self.assertTrue(window.passkey_waiting_label.get_visible())
        self.assertEqual(window.passkey_cancel_button.get_label(), "Cancel")
        self.assertEqual(window.passkey_button.title, "Waiting…")
        self.assertFalse(window.passkey_button.get_sensitive())

        self.login.passkey_done.set()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))
        self.assertFalse(window.get_visible())

    def test_passkey_cancel_restarts(self):
        self.login.srp = NeedsPasskey("pk-sess", "https://accounts.ente.io")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("passkey")
        original = self.login_module.Gtk.show_uri_on_window
        self.login_module.Gtk.show_uri_on_window = lambda parent, uri, timestamp: None
        try:
            window.passkey_button.clicked()
            pump(50)
        finally:
            self.login_module.Gtk.show_uri_on_window = original
        self.assertTrue(wait_until(lambda: self.model.is_busy))
        window.passkey_cancel_button.clicked()
        pump(50)
        self.assertEqual(window.visible_page, "credentials")
        self.assertFalse(self.model.is_busy)
        self.assertIsNone(window.error_text)
        pump(100)  # the cancelled poll winds down without surfacing anything
        self.assertIsNone(window.error_text)
        self.assertEqual(self.app.presented, 0)

    def test_browser_failure_is_logged_and_the_wait_still_starts(self):
        self.login.srp = NeedsPasskey("pk-sess", "https://accounts.ente.io")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.wait_for_page("passkey")
        GLib = self.login_module.GLib

        def failing(parent, uri, timestamp):
            raise GLib.Error("no browser")
        original = self.login_module.Gtk.show_uri_on_window
        self.login_module.Gtk.show_uri_on_window = failing
        try:
            with self.assertLogs("gans.app", level="WARNING"):
                window.passkey_button.clicked()
                pump(50)
        finally:
            self.login_module.Gtk.show_uri_on_window = original
        self.assertTrue(wait_until(lambda: ("passkey", "pk-sess") in self.login.calls))
        self.login.passkey_done.set()
        self.assertTrue(wait_until(lambda: self.app.presented == 1))

    # MARK: Errors

    def test_error_shows_in_red_and_clears_on_the_next_attempt(self):
        self.login.srp = LoginError("Wrong password.")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.assertTrue(wait_until(lambda: window.error_text == "Wrong password."))
        self.assertTrue(window.sign_in_button.get_sensitive())
        self.assertEqual(window.visible_page, "credentials")
        self.login.srp = NeedsEmailCode()
        window.sign_in_button.clicked()
        self.wait_for_page("code")
        self.assertIsNone(window.error_text)

    def test_error_text_is_escaped(self):
        self.login.srp = LoginError("<b>bold</b> & co")
        window = self.show()
        self.fill_credentials(window)
        window.sign_in_button.clicked()
        self.assertTrue(wait_until(lambda: window.error_text == "<b>bold</b> & co"))

    def test_signed_in_without_an_app(self):
        controller = self.login_module.LoginWindowController(self.vault, api=None, app=None, login=self.login)
        self.addCleanup(lambda: controller.window is not None and controller.window.destroy())
        controller.show()
        self.assertTrue(wait_until(lambda: controller.window.get_mapped()))
        self.fill_credentials(controller.window)
        controller.window.sign_in_button.clicked()
        self.assertTrue(wait_until(lambda: not controller.window.get_visible()))
        self.assertEqual(len(self.vault.logins), 1)


if __name__ == "__main__":
    unittest.main()

"""``LoginViewModel`` against a scripted ``EnteLogin`` stand-in and a recording vault, with
``dispatch`` calling straight through: every action runs on a worker thread and lands its
result back in the model, so the tests wait on ``is_busy`` (the last write of each
completion) or on the callbacks the model fires."""

import threading
import time
import unittest

from gans.crypto import CryptoError
from gans.ente.login import Authorized, LoginError, NeedsEmailCode, NeedsPasskey, NeedsTwoFactor
from gans.ente.models import AuthorizationResponse
from gans.ente.vault import VaultError
from gans.ui.login_model import LoginViewModel, Stage

AUTH = AuthorizationResponse(id=1, encrypted_token="tok")


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# MARK: Fakes

class FakeLogin:
    """Scripted ``EnteLogin``: each method returns (or raises) what the test queued, and
    records its arguments. ``wait_for_passkey_token`` blocks until ``passkey_done`` is set
    or the model cancels it, like the real poll."""

    def __init__(self):
        self.calls = []
        self.srp = Authorized(AUTH)
        self.email_otp = NeedsEmailCode()
        self.verify_email = Authorized(AUTH)
        self.verify_two_factor_result = Authorized(AUTH)
        self.passkey = Authorized(AUTH)
        self.passkey_done = threading.Event()
        self.passkey_cancelled = False
        #: When set, ``start_srp`` blocks on it (to keep the model busy on purpose).
        self.srp_gate = None

    def _result(self, value):
        if isinstance(value, BaseException):
            raise value
        return value

    def start_srp(self, email, password):
        self.calls.append(("srp", email, password))
        if self.srp_gate is not None:
            self.srp_gate.wait()
        return self._result(self.srp)

    def send_email_otp(self, email):
        self.calls.append(("otp", email))
        return self._result(self.email_otp)

    def verify_email_otp(self, email, code):
        self.calls.append(("verify_email", email, code))
        return self._result(self.verify_email)

    def verify_two_factor(self, session_id, code):
        self.calls.append(("verify_2fa", session_id, code))
        return self._result(self.verify_two_factor_result)

    def wait_for_passkey_token(self, session_id, timeout=180, poll_interval=2, is_cancelled=lambda: False):
        self.calls.append(("passkey", session_id))
        while not self.passkey_done.is_set():
            if is_cancelled():
                self.passkey_cancelled = True
                raise InterruptedError("passkey wait cancelled")
            time.sleep(0.01)
        return self._result(self.passkey)


class FakeVault:
    def __init__(self):
        self.logins = []
        self.error = None

    def complete_login(self, authorization, password, email):
        self.logins.append((authorization, password, email))
        if self.error is not None:
            raise self.error


class LoginViewModelTests(unittest.TestCase):
    def setUp(self):
        self.login = FakeLogin()
        self.vault = FakeVault()
        self.changes = 0
        self.signed_in = 0
        self.model = LoginViewModel(self.vault, api=None, dispatch=lambda fn: fn(), login=self.login)
        self.model.on_change(self._changed)
        self.model.on_signed_in = self._on_signed_in
        self.model.email = "  alice@example.com "
        self.model.password = "hunter2"

    def _changed(self):
        self.changes += 1

    def _on_signed_in(self):
        self.signed_in += 1

    def settle(self):
        self.assertTrue(wait_for(lambda: not self.model.is_busy), "the action never finished")

    # MARK: Credentials

    def test_can_submit_credentials(self):
        self.assertEqual(self.model.trimmed_email, "alice@example.com")
        self.assertTrue(self.model.can_submit_credentials)
        self.model.password = ""
        self.assertFalse(self.model.can_submit_credentials)
        self.model.password = "x"
        self.model.email = "   "
        self.assertFalse(self.model.can_submit_credentials)

    def test_srp_happy_path_completes_login_and_clears_the_password(self):
        self.model.sign_in_with_password()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(self.login.calls, [("srp", "alice@example.com", "hunter2")])
        self.assertEqual(self.vault.logins, [(AUTH, "hunter2", "alice@example.com")])
        self.assertEqual(self.model.password, "")  # dropped once the keys are unwrapped
        self.assertIsNone(self.model.error_message)
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertGreaterEqual(self.changes, 2)  # busy on, busy off

    def test_srp_unavailable_falls_back_to_the_email_code(self):
        self.login.srp = LoginError(LoginError.SRP_UNAVAILABLE)
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual([call[0] for call in self.login.calls], ["srp", "otp"])
        self.assertEqual(self.model.stage, Stage.email_code())
        self.assertTrue(self.model.stage.is_email_code)
        self.assertIsNone(self.model.error_message)
        self.assertEqual(self.vault.logins, [])
        self.assertEqual(self.signed_in, 0)

    def test_other_login_errors_are_not_retried_by_email(self):
        self.login.srp = LoginError("Wrong password.")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual([call[0] for call in self.login.calls], ["srp"])
        self.assertEqual(self.model.error_message, "Wrong password.")
        self.assertEqual(self.model.stage, Stage.credentials())

    def test_send_email_code_explicitly(self):
        self.model.send_email_code()
        self.settle()
        self.assertEqual(self.login.calls, [("otp", "alice@example.com")])
        self.assertEqual(self.model.stage, Stage.email_code())

    # MARK: Codes

    def test_submit_code_in_email_stage(self):
        self.login.srp = LoginError(LoginError.SRP_UNAVAILABLE)
        self.model.sign_in_with_password()
        self.settle()
        self.model.code = " 123456 "
        self.model.submit_code()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(self.login.calls[-1], ("verify_email", "alice@example.com", " 123456 "))
        self.assertEqual(self.vault.logins, [(AUTH, "hunter2", "alice@example.com")])

    def test_submit_code_in_two_factor_stage(self):
        self.login.srp = NeedsTwoFactor("sess-2fa")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.model.stage, Stage.two_factor("sess-2fa"))
        self.assertEqual(self.model.stage.session_id, "sess-2fa")
        self.model.code = "654321"
        self.model.submit_code()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(self.login.calls[-1], ("verify_2fa", "sess-2fa", "654321"))

    def test_signing_in_from_a_code_stage_resets_to_credentials(self):
        # The model outlives the session (the window is hidden, not destroyed), so a
        # later sign-in must start over rather than on the previous login's dead 2FA
        # session; the email stays for convenience, the password and code are dropped.
        self.login.srp = NeedsTwoFactor("sess-2fa")
        self.model.sign_in_with_password()
        self.settle()
        self.model.code = "654321"
        self.model.submit_code()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertEqual(self.model.code, "")
        self.assertEqual(self.model.password, "")
        self.assertEqual(self.model.email, "  alice@example.com ")
        self.assertIsNone(self.model.error_message)
        # A second sign-in goes through the credentials flow again, not the old session.
        self.model.password = "hunter2"
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.login.calls[-1], ("srp", "alice@example.com", "hunter2"))

    def test_submit_code_outside_a_code_stage_does_nothing(self):
        self.model.code = "123456"
        self.model.submit_code()
        self.assertFalse(self.model.is_busy)
        self.assertEqual(self.login.calls, [])

    def test_moving_to_a_code_stage_clears_a_stale_code(self):
        self.model.code = "old"
        self.model.send_email_code()
        self.settle()
        self.assertEqual(self.model.code, "")

    # MARK: Passkey

    def test_passkey_stage_waits_then_completes(self):
        self.login.srp = NeedsPasskey("pk-sess", "https://accounts.ente.io")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.model.stage, Stage.passkey("pk-sess", "https://accounts.ente.io"))
        url = self.model.passkey_verification_url
        self.assertIn("passkeySessionID=pk-sess", url)
        self.assertTrue(url.startswith("https://accounts.ente.io/passkeys/verify?"))

        self.model.wait_for_passkey()
        self.assertTrue(wait_for(lambda: ("passkey", "pk-sess") in self.login.calls))
        self.assertTrue(self.model.is_busy)
        self.login.passkey_done.set()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(self.vault.logins, [(AUTH, "hunter2", "alice@example.com")])
        self.assertEqual(self.model.password, "")

    def test_passkey_url_is_none_outside_the_passkey_stage(self):
        self.assertIsNone(self.model.passkey_verification_url)
        self.model.wait_for_passkey()
        self.assertFalse(self.model.is_busy)
        self.assertEqual(self.login.calls, [])

    def test_restart_cancels_a_pending_passkey_wait(self):
        self.login.srp = NeedsPasskey("pk-sess", "https://accounts.ente.io")
        self.model.sign_in_with_password()
        self.settle()
        self.model.wait_for_passkey()
        self.assertTrue(wait_for(lambda: ("passkey", "pk-sess") in self.login.calls))
        self.model.code = "x"
        self.model.restart()
        self.assertFalse(self.model.is_busy)
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertEqual(self.model.code, "")
        self.assertIsNone(self.model.error_message)
        self.assertTrue(wait_for(lambda: self.login.passkey_cancelled))
        # The cancelled worker's exit is not an error and doesn't disturb the fresh state.
        time.sleep(0.05)
        self.assertIsNone(self.model.error_message)
        self.assertFalse(self.model.is_busy)
        self.assertEqual(self.signed_in, 0)
        self.assertEqual(self.vault.logins, [])

    def test_a_late_result_from_a_cancelled_task_does_not_move_the_stage(self):
        self.login.srp_gate = threading.Event()
        self.login.srp = NeedsTwoFactor("late")
        self.model.sign_in_with_password()
        self.assertTrue(wait_for(lambda: len(self.login.calls) == 1))
        self.model.restart()
        self.login.srp_gate.set()
        time.sleep(0.1)
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertFalse(self.model.is_busy)

    # MARK: Errors and busy handling

    def test_errors_surface_and_busy_resets(self):
        self.login.srp = LoginError("Boom")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.model.error_message, "Boom")
        self.assertFalse(self.model.is_busy)
        self.assertEqual(self.signed_in, 0)
        # The next attempt clears the previous error.
        self.login.srp = Authorized(AUTH)
        self.model.sign_in_with_password()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.assertIsNone(self.model.error_message)

    def test_vault_failure_is_reported_and_keeps_the_password(self):
        self.vault.error = VaultError(VaultError.NO_AUTHENTICATOR_DATA)
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.model.error_message, VaultError.NO_AUTHENTICATOR_DATA)
        self.assertEqual(self.model.password, "hunter2")
        self.assertEqual(self.signed_in, 0)

    def test_cancelled_vault_login_is_not_reported_as_success(self):
        self.vault.error = InterruptedError("Session changed during login")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.signed_in, 0)
        self.assertEqual(self.model.stage, Stage.credentials())
        self.assertIsNone(self.model.error_message)

    def test_crypto_failure_message(self):
        self.vault.error = CryptoError("operation_failed", "key unwrap")
        self.model.sign_in_with_password()
        self.settle()
        self.assertEqual(self.model.error_message, str(CryptoError("operation_failed", "key unwrap")))

    def test_unexpected_exception_still_surfaces(self):
        self.login.srp = KeyError("keyAttributes")
        with self.assertLogs("gans.app", level="ERROR"):
            self.model.sign_in_with_password()
            self.settle()
        self.assertEqual(self.model.error_message, "'keyAttributes'")

    def test_a_second_action_while_busy_is_ignored(self):
        self.login.srp_gate = threading.Event()
        self.model.sign_in_with_password()
        self.assertTrue(wait_for(lambda: len(self.login.calls) == 1))
        self.assertTrue(self.model.is_busy)
        self.model.send_email_code()
        self.model.sign_in_with_password()
        self.model.code = "1"
        self.model.submit_code()
        self.assertEqual(len(self.login.calls), 1)
        self.login.srp_gate.set()
        self.assertTrue(wait_for(lambda: self.signed_in == 1))
        self.settle()
        self.assertEqual(len(self.login.calls), 1)

    def test_observers_are_notified_on_every_state_change(self):
        self.login.srp = NeedsEmailCode()
        before = self.changes
        self.model.sign_in_with_password()
        self.settle()
        self.assertGreaterEqual(self.changes - before, 2)
        before = self.changes
        self.model.restart()
        self.assertEqual(self.changes - before, 1)

    def test_default_login_is_built_from_the_api(self):
        from gans.ente.api import EnteAPI
        from gans.ente.login import EnteLogin
        model = LoginViewModel(self.vault, EnteAPI(), dispatch=lambda fn: fn())
        self.assertIsInstance(model._login, EnteLogin)


class StageTests(unittest.TestCase):
    def test_constructors_and_predicates(self):
        self.assertTrue(Stage.credentials().is_credentials)
        self.assertTrue(Stage.email_code().is_email_code)
        two_factor = Stage.two_factor("s")
        self.assertTrue(two_factor.is_two_factor)
        self.assertEqual(two_factor.session_id, "s")
        passkey = Stage.passkey("p", "https://accounts.ente.io")
        self.assertTrue(passkey.is_passkey)
        self.assertEqual((passkey.session_id, passkey.accounts_url), ("p", "https://accounts.ente.io"))
        self.assertNotEqual(Stage.two_factor("a"), Stage.two_factor("b"))
        with self.assertRaises(AttributeError):
            passkey.kind = "x"  # frozen


if __name__ == "__main__":
    unittest.main()

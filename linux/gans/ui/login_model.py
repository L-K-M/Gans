"""Drives the login UI through Ente's steps and, on success, hands the authorization to
the vault to unwrap keys and persist the session — the port of ``LoginViewModel.swift``.

Plain Python (no GTK) so the flow is unit-testable headless. Every action runs on **one**
worker thread at a time (``EnteLogin`` and ``EnteVault.complete_login`` block on the
network and Argon2id); the worker marshals stage/error updates back through the
``dispatch`` callable, so observers always run on the GTK main thread. ``restart()`` is
the one thing that can interrupt a worker: it flags the in-flight task as cancelled,
which the passkey poll checks between requests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

from .. import log
from ..crypto import CryptoError
from ..ente.api import APIError, EnteAPI
from ..ente.login import (Authorized, EnteLogin, LoginError, NeedsEmailCode, NeedsPasskey, NeedsTwoFactor,
                          Step)
from ..ente.vault import EnteVault, VaultError

__all__ = ["LoginViewModel", "Stage"]

Dispatch = Callable[[Callable[[], object]], object]

#: Failures with a user-facing message; anything else is a bug and gets a traceback too.
_EXPECTED_ERRORS = (LoginError, APIError, VaultError, CryptoError, OSError, ValueError)


# MARK: Stage

@dataclass(frozen=True)
class Stage:
    """Which page the login window shows. ``session_id`` is the 2FA session for
    ``TWO_FACTOR`` and the passkey session for ``PASSKEY``; ``accounts_url`` is set for
    ``PASSKEY`` only."""

    CREDENTIALS = "credentials"
    EMAIL_CODE = "emailCode"
    TWO_FACTOR = "twoFactor"
    PASSKEY = "passkey"

    kind: str
    session_id: Optional[str] = None
    accounts_url: Optional[str] = None

    @classmethod
    def credentials(cls) -> "Stage":
        return cls(cls.CREDENTIALS)

    @classmethod
    def email_code(cls) -> "Stage":
        return cls(cls.EMAIL_CODE)

    @classmethod
    def two_factor(cls, session_id: str) -> "Stage":
        return cls(cls.TWO_FACTOR, session_id=session_id)

    @classmethod
    def passkey(cls, passkey_session_id: str, accounts_url: str) -> "Stage":
        return cls(cls.PASSKEY, session_id=passkey_session_id, accounts_url=accounts_url)

    @property
    def is_credentials(self) -> bool:
        return self.kind == self.CREDENTIALS

    @property
    def is_email_code(self) -> bool:
        return self.kind == self.EMAIL_CODE

    @property
    def is_two_factor(self) -> bool:
        return self.kind == self.TWO_FACTOR

    @property
    def is_passkey(self) -> bool:
        return self.kind == self.PASSKEY


class _Task:
    """One in-flight action. ``cancelled`` is read by the worker (the passkey poll) and by
    the completion handler, so a task the user abandoned can't move the UI forward."""

    def __init__(self) -> None:
        self.cancelled = False


# MARK: View model

class LoginViewModel:
    def __init__(self, vault: EnteVault, api: EnteAPI, dispatch: Dispatch, login: Optional[EnteLogin] = None):
        self._vault = vault
        self._login = login if login is not None else EnteLogin(api)
        self._dispatch = dispatch
        self._observers: List[Callable[[], None]] = []
        self._task: Optional[_Task] = None

        #: Written by the view as the user types.
        self.email = ""
        self.password = ""
        self.code = ""

        self.stage: Stage = Stage.credentials()
        self.is_busy = False
        self.error_message: Optional[str] = None
        #: Called once a session is fully established.
        self.on_signed_in: Callable[[], object] = lambda: None

    # MARK: Observers

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        """Runs the observers. Only ever called from main-thread code (actions start there,
        and worker results arrive through ``dispatch``), so observers see consistent state."""
        for callback in list(self._observers):
            try:
                callback()
            except Exception:
                log.app.exception("Login observer failed")

    # MARK: Derived state

    @property
    def trimmed_email(self) -> str:
        return self.email.strip()

    @property
    def can_submit_credentials(self) -> bool:
        return bool(self.trimmed_email) and bool(self.password) and not self.is_busy

    # MARK: Actions

    def sign_in_with_password(self) -> None:
        """Tries SRP first; on accounts that require email verification it transparently
        falls back to the email-code flow."""
        email, password = self.trimmed_email, self.password

        def work(_task: _Task) -> Step:
            try:
                return self._login.start_srp(email, password)
            except LoginError as error:
                if not error.is_srp_unavailable:
                    raise
                return self._login.send_email_otp(email)
        self._run(work)

    def send_email_code(self) -> None:
        """Explicitly requests an email code instead of using SRP."""
        email = self.trimmed_email
        self._run(lambda _task: self._login.send_email_otp(email))

    def submit_code(self) -> None:
        stage, email, code = self.stage, self.trimmed_email, self.code
        if stage.is_email_code:
            self._run(lambda _task: self._login.verify_email_otp(email, code))
        elif stage.is_two_factor and stage.session_id is not None:
            session_id = stage.session_id
            self._run(lambda _task: self._login.verify_two_factor(session_id, code))

    def restart(self) -> None:
        """Back to the credentials page, abandoning whatever is in flight (a long passkey
        wait in particular)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancelled = True
        self.is_busy = False
        self.stage = Stage.credentials()
        self.code = ""
        self.error_message = None
        self._notify()

    # MARK: Passkey

    @property
    def passkey_verification_url(self) -> Optional[str]:
        """The browser URL that runs the passkey ceremony for the current passkey stage."""
        stage = self.stage
        if not stage.is_passkey or stage.session_id is None:
            return None
        return EnteLogin.passkey_verification_url(stage.accounts_url or "", stage.session_id, EnteAPI.CLIENT_PACKAGE)

    def wait_for_passkey(self) -> None:
        """Begins waiting for the browser passkey ceremony to finish (polls for the token).
        The view opens ``passkey_verification_url`` and then calls this."""
        stage = self.stage
        if not stage.is_passkey or stage.session_id is None:
            return
        session_id = stage.session_id
        self._run(lambda task: self._login.wait_for_passkey_token(session_id, is_cancelled=lambda: task.cancelled))

    # MARK: Running work

    def _run(self, work: Callable[[_Task], Step]) -> None:
        """Runs ``work`` on a worker thread with busy/error bookkeeping. Ignored while
        another action is in flight."""
        if self.is_busy:
            return
        task = _Task()
        self._task = task
        self.is_busy = True
        self.error_message = None
        self._notify()
        password, email = self.password, self.trimmed_email

        def worker() -> None:
            step: Optional[Step] = None
            error: Optional[BaseException] = None
            try:
                step = work(task)
                if isinstance(step, Authorized):
                    # Still on the worker: unwrapping keys and the first sync block too.
                    self._vault.complete_login(step.authorization, password, email)
            except InterruptedError:
                # Authorization alone is not a completed, persisted session.
                step = None
            except _EXPECTED_ERRORS as failure:
                error = failure
            except Exception as failure:
                log.app.exception("Login action failed unexpectedly")
                error = failure
            self._dispatch(lambda: self._finish(task, step, error))

        threading.Thread(target=worker, name="gans-login", daemon=True).start()

    def _finish(self, task: _Task, step: Optional[Step], error: Optional[BaseException]) -> bool:
        """Applies a worker's outcome on the main thread. ``is_busy`` is the last write so
        anyone polling it sees the rest of the state settled."""
        if error is None and isinstance(step, Authorized):
            # The password has unwrapped the key hierarchy and is no longer needed; drop
            # our reference so it doesn't linger in memory for the app's lifetime. A
            # session that completed after a late Cancel still counts: the vault is
            # signed in, so the window must close either way.
            self.password = ""
            # Back to the credentials page: the controller keeps this model (and its
            # window) across hide/show, so after Sign Out -> "Sign in to Ente..." the
            # window must not re-open on the 2FA/email-code page of a dead session.
            self.code = ""
            self.stage = Stage.credentials()
            self.error_message = None
            self._clear_busy(task)
            self._notify()
            self.on_signed_in()
            return False
        if task.cancelled:
            # The user went back to the credentials page; a late result mustn't move
            # them forward again (the stale task's busy flag was cleared by restart()).
            return False
        if error is not None:
            self.error_message = str(error) or error.__class__.__name__
        elif isinstance(step, NeedsEmailCode):
            self.code = ""
            self.stage = Stage.email_code()
        elif isinstance(step, NeedsTwoFactor):
            self.code = ""
            self.stage = Stage.two_factor(step.session_id)
        elif isinstance(step, NeedsPasskey):
            self.stage = Stage.passkey(step.passkey_session_id, step.accounts_url)
        self._clear_busy(task)
        self._notify()
        return False  # so GLib.idle_add doesn't repeat

    def _clear_busy(self, task: _Task) -> None:
        if task is self._task:
            self._task = None
            self.is_busy = False

"""Optional app-level lock — the Linux counterpart of ``AppLock.swift``.

When enabled (``Preferences.require_unlock``), Gans starts **locked**: the session isn't
restored and codes aren't shown until the user proves it's them. macOS asks for Touch ID
or the device password through ``LAContext``; here the desktop's polkit agent asks for the
user's own password through the ``ch.lkmc.gans.unlock`` action (``auth_self``, installed
with the package), driven by ``pkcheck --allow-user-interaction``.

The session token and authenticator key already sit in the Secret Service; this adds a
second gate so an unlocked desktop left unattended doesn't hand out codes.

``pkcheck`` blocks while the agent's dialog is up, so it runs on a worker thread and the
verdict is marshalled back through ``dispatch``. Its exit status decides:

* ``0`` — authorized → unlock.
* ``1`` with "Not authorized" / "authorization failed", or a dismissed dialog → stay
  locked: the user (or the policy) actively said no.
* anything else — ``pkcheck`` missing, the action not registered (running from the
  source tree), no authentication agent in this session, no polkit authority, a crash —
  → unlock with a logged warning. The macOS build makes the same call when no device
  authentication exists: a lock nobody can open is worse than none.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Callable, List, Optional, Tuple

from .. import log
from ..prefs import Preferences

__all__ = ["AppLock"]

Dispatch = Callable[[Callable[[], object]], object]
Completion = Callable[[bool], object]


class AppLock:
    ACTION_ID = "ch.lkmc.gans.unlock"
    DEFAULT_REASON = "Unlock Gans to access your codes"
    #: The polkit agent's own dialog has no timeout; this bounds a wedged agent. Expiry
    #: counts as a dismissal (the prompt was shown and went unanswered), never as a pass.
    PROMPT_TIMEOUT = 300.0
    _DENIED_PHRASES = ("not authorized", "authorization failed")

    def __init__(self, prefs: Preferences, dispatch: Dispatch):
        self._prefs = prefs
        self._dispatch = dispatch
        self._is_locked = False
        self._authenticating = False
        self._observers: List[Callable[[], None]] = []

    # MARK: State

    @property
    def is_locked(self) -> bool:
        """True when the app is locked and the vault must not be used yet."""
        return self._is_locked

    @property
    def is_enabled(self) -> bool:
        return self._prefs.require_unlock

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _set_locked(self, locked: bool) -> None:
        if self._is_locked == locked:
            return
        self._is_locked = locked
        for callback in list(self._observers):
            try:
                callback()
            except Exception:
                log.app.exception("AppLock observer failed")

    def lock_if_enabled(self) -> None:
        """Engages the lock if the feature is on. Call at launch (when a session exists)."""
        if self._prefs.require_unlock:
            self._set_locked(True)

    def lock(self) -> None:
        """Locks immediately, regardless of the setting — the manual "Lock Now" action.
        Unlocking still requires the password."""
        self._set_locked(True)

    # MARK: Authentication

    def authenticate(self, reason: str = DEFAULT_REASON, completion: Optional[Completion] = None) -> None:
        """Prompts through polkit and unlocks on success. Re-entrant calls while a prompt is
        showing are answered ``False`` immediately; ``completion`` runs via ``dispatch``."""
        if not self._is_locked:
            if completion is not None:
                completion(True)
            return
        if self._authenticating:
            if completion is not None:
                completion(False)
            return
        self._authenticating = True
        log.app.info("Requesting unlock: %s", reason)
        worker = threading.Thread(target=self._run_prompt, args=(completion,), name="gans-applock", daemon=True)
        worker.start()

    def _run_prompt(self, completion: Optional[Completion]) -> None:
        try:
            unlock, warning = self._check_authorization()
        except Exception:
            # Never strand the caller: ``finish`` must run so the guard resets and the
            # completion fires exactly once (``LAContext`` always calls back, too).
            log.app.exception("The unlock check failed; staying locked")
            unlock, warning = False, None

        def finish() -> bool:
            self._authenticating = False
            if warning:
                log.app.warning("Unlocking without a password check: %s", warning)
            if unlock:
                self._set_locked(False)
            if completion is not None:
                completion(unlock)
            return False   # so GLib.idle_add doesn't repeat

        self._dispatch(finish)

    @classmethod
    def _pkcheck_binary(cls) -> Optional[str]:
        return os.environ.get("GANS_PKCHECK") or shutil.which("pkcheck")

    @classmethod
    def _check_authorization(cls) -> Tuple[bool, Optional[str]]:
        """Runs ``pkcheck`` (blocking). Returns ``(unlock, warning)``: ``warning`` is set when
        we unlock *without* a real check so the log says why."""
        binary = cls._pkcheck_binary()
        if binary is None:
            return True, "pkcheck (polkit) isn't installed"
        command = [binary, "--action-id", cls.ACTION_ID, "--process", str(os.getpid()), "--allow-user-interaction"]
        try:
            # errors="replace": a localized agent message in another encoding must not raise.
            result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                    timeout=cls.PROMPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.app.warning("The polkit prompt went unanswered for %.0f s; staying locked", cls.PROMPT_TIMEOUT)
            return False, None
        except OSError as error:
            return True, f"couldn't run pkcheck: {error}"
        return cls._classify(result.returncode, (result.stdout or "") + (result.stderr or ""))

    @classmethod
    def _classify(cls, returncode: int, output: str) -> Tuple[bool, Optional[str]]:
        if returncode == 0:
            return True, None
        lowered = output.lower()
        if "dismissed" in lowered or (returncode == 1 and any(phrase in lowered for phrase in cls._DENIED_PHRASES)):
            log.app.info("Unlock refused by polkit (exit %s)", returncode)
            return False, None
        detail = " ".join(output.split()) or "no output"
        return True, f"pkcheck exited {returncode}: {detail}"

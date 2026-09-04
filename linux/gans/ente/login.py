"""Drives Ente's interactive login. The password is always required (it unwraps the key
hierarchy regardless of how the session is authenticated), so the UI collects email +
password up front, then this coordinator picks the best path:

 1. **SRP-6a** when the account has it and email-MFA is off — no email round-trip.
 2. **Email-OTP** otherwise (or if SRP fails) — Ente emails a code to verify.
 3. **Account 2FA** (TOTP) or a **passkey** when the chosen path asks for a second factor.

All branches end in an ``AuthorizationResponse`` carrying ``keyAttributes`` +
``encryptedToken``, which ``keyunwrap`` turns into keys + token. Every method blocks —
call from a worker thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from .. import b64, crypto, log
from .api import APIError, EnteAPI
from .models import AuthorizationResponse, CreateSRPSessionResponse, SRPAttributes
from .srp import EnteSRP

__all__ = ["EnteLogin", "LoginError", "Step", "Authorized", "NeedsEmailCode", "NeedsTwoFactor", "NeedsPasskey"]


class Step:
    """The next thing the UI must collect before a session can be produced."""


@dataclass
class Authorized(Step):
    """Authentication is complete; unwrap keys with the password."""

    authorization: AuthorizationResponse


@dataclass
class NeedsEmailCode(Step):
    """Ente emailed a code; call ``verify_email_otp``."""


@dataclass
class NeedsTwoFactor(Step):
    """Account-level 2FA; call ``verify_two_factor`` with ``session_id``."""

    session_id: str


@dataclass
class NeedsPasskey(Step):
    """The account's second factor is a passkey: open the verification URL in a browser,
    then poll for the token. Carries what both steps need."""

    passkey_session_id: str
    accounts_url: str


class LoginError(Exception):
    SRP_UNAVAILABLE = "SRP login isn't available for this account."
    PASSKEY_TIMED_OUT = "Timed out waiting for passkey authentication. Please try again."

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    @property
    def is_srp_unavailable(self) -> bool:
        return self.message == self.SRP_UNAVAILABLE


class EnteLogin:
    #: The canonical Ente accounts host, used when the server-supplied value can't be trusted.
    DEFAULT_ACCOUNTS_URL = "https://accounts.ente.io"

    def __init__(self, api: EnteAPI):
        self._api = api

    # MARK: SRP path

    def start_srp(self, email: str, password: str) -> Step:
        """Attempts SRP login. Raises ``LoginError`` (``is_srp_unavailable``) if SRP isn't
        usable — the caller should fall back to email."""
        attributes = SRPAttributes.from_json(
            self._api.get("users/srp/attributes", [("email", email)], authenticated=False))
        if attributes.is_email_mfa_enabled:
            raise LoginError(LoginError.SRP_UNAVAILABLE)  # account prefers email verification
        kek_salt = b64.decode_standard(attributes.kek_salt)
        srp_salt = b64.decode_standard(attributes.srp_salt)
        if kek_salt is None or srp_salt is None:
            raise LoginError(LoginError.SRP_UNAVAILABLE)

        # loginKey = first 16 bytes of KDF(Argon2id(password, kekSalt)).
        kek = crypto.derive_key_encryption_key(password, kek_salt, attributes.mem_limit, attributes.ops_limit)
        login_key = crypto.derive_login_key(kek)
        del kek

        handshake = EnteSRP.begin(attributes.srp_user_id, srp_salt, login_key)
        del login_key
        created = CreateSRPSessionResponse.from_json(self._api.post(
            "users/srp/create-session",
            {"srpUserID": attributes.srp_user_id, "srpA": handshake.srp_a_base64},
            authenticated=False))
        m1 = handshake.compute_m1(created.srp_b)

        auth = AuthorizationResponse.from_json(self._api.post(
            "users/srp/verify-session",
            {"srpUserID": attributes.srp_user_id, "sessionID": created.session_id, "srpM1": m1},
            authenticated=False))

        # Verify the server's proof (M2) when present, completing SRP mutual auth. A
        # mismatch is logged rather than fatal: the key unwrap that follows is the
        # authoritative check (a server that didn't know the verifier can't produce a
        # token we can decrypt).
        if auth.srp_m2 and not handshake.verify_server_proof(auth.srp_m2, m1):
            log.ente.error("SRP server proof (M2) did not verify; relying on key-unwrap to reject an impostor server.")
        return self._step(auth)

    # MARK: Email-OTP path

    def send_email_otp(self, email: str) -> Step:
        """Requests an email login code (``/users/ott``)."""
        self._api.post("users/ott", {"email": email, "purpose": "login"}, authenticated=False)
        return NeedsEmailCode()

    def verify_email_otp(self, email: str, code: str) -> Step:
        """Verifies the emailed code (``/users/verify-email``)."""
        auth = AuthorizationResponse.from_json(self._api.post(
            "users/verify-email", {"email": email, "ott": code.strip()}, authenticated=False))
        return self._step(auth)

    # MARK: 2FA

    def verify_two_factor(self, session_id: str, code: str) -> Step:
        """Verifies an account-level TOTP code (``/users/two-factor/verify``)."""
        auth = AuthorizationResponse.from_json(self._api.post(
            "users/two-factor/verify", {"sessionID": session_id, "code": code.strip()}, authenticated=False))
        return self._step(auth)

    # MARK: Helpers

    def _step(self, auth: AuthorizationResponse) -> Step:
        if auth.requires_passkey and auth.passkey_session_id:
            return NeedsPasskey(auth.passkey_session_id, auth.accounts_url or self.DEFAULT_ACCOUNTS_URL)
        if auth.requires_two_factor and auth.two_factor_session_id:
            return NeedsTwoFactor(auth.two_factor_session_id)
        return Authorized(auth)

    # MARK: Passkey (second factor)

    @classmethod
    def passkey_verification_url(cls, accounts_url: str, passkey_session_id: str, client_package: str) -> Optional[str]:
        """The accounts page that runs the WebAuthn passkey ceremony for this login session.
        Mirrors Ente's CLI: ``…/passkeys/verify?passkeySessionID=…&redirect=…&clientPackage=…``.
        We don't handle the redirect ourselves — ``redirect`` only has to be a value the
        accounts page whitelists (``ente-cli://passkey`` is), because we retrieve the token
        by polling ``get-token`` rather than via the browser redirect."""
        base = urlsplit(cls.sanitized_accounts_base(accounts_url))
        query = urlencode([("passkeySessionID", passkey_session_id),
                           ("redirect", "ente-cli://passkey"),
                           ("clientPackage", client_package)])
        return urlunsplit((base.scheme, base.netloc, "/passkeys/verify", query, ""))

    @classmethod
    def sanitized_accounts_base(cls, raw: str) -> str:
        """``accountsUrl`` comes from the login response, so a hostile/MITM server could try
        to point the browser at an attacker-controlled page. Only honor its host when it's
        an HTTPS Ente host; otherwise fall back to the canonical accounts host."""
        try:
            components = urlsplit(raw or "")
        except ValueError:
            return cls.DEFAULT_ACCOUNTS_URL
        host = (components.hostname or "").lower()
        if components.scheme.lower() != "https" or not host:
            return cls.DEFAULT_ACCOUNTS_URL
        if host == "ente.io" or host.endswith(".ente.io"):
            # Rebuild from the validated host only: userinfo, a non-default port, path,
            # query and fragment that rode along in `raw` must not reach the browser.
            return urlunsplit(("https", host, "", "", ""))
        return cls.DEFAULT_ACCOUNTS_URL

    def wait_for_passkey_token(self, passkey_session_id: str, timeout: float = 180, poll_interval: float = 2,
                               is_cancelled: Callable[[], bool] = lambda: False) -> Step:
        """Polls ``get-token`` until the browser passkey ceremony completes and the server has
        an authorization for this session, then returns the authorized step. Raises
        ``LoginError`` on timeout and ``InterruptedError`` when cancelled."""
        deadline = time.monotonic() + timeout
        while True:
            if is_cancelled():
                raise InterruptedError("passkey wait cancelled")
            # Until the ceremony finishes, this 404s / lacks key attributes; keep waiting.
            try:
                auth = AuthorizationResponse.from_json(self._api.get(
                    "users/two-factor/passkeys/get-token", [("sessionID", passkey_session_id)], authenticated=False))
            except (APIError, ValueError):
                auth = None
            if auth is not None and auth.key_attributes is not None and (auth.encrypted_token or auth.token):
                return Authorized(auth)
            if time.monotonic() >= deadline:
                raise LoginError(LoginError.PASSKEY_TIMED_OUT)
            # Sleep in small slices so cancellation is prompt.
            slept = 0.0
            while slept < poll_interval:
                if is_cancelled():
                    raise InterruptedError("passkey wait cancelled")
                time.sleep(0.2)
                slept += 0.2

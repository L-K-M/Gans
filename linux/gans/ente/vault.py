"""The app's single source of truth for the Ente session and the decrypted entries.

Security model: the keyring holds the auth token + the 32-byte authenticator key; the disk
cache holds only Ente's encrypted entity blobs; the password and plaintext secrets live
solely in memory. On launch we decrypt the cache (instant menu), then refresh from the
network.

All the blocking methods (``restore``, ``complete_login``, ``refresh``) are meant to run on
a worker thread. State is updated atomically and observers are notified through the
``dispatch`` callable (``GLib.idle_add`` in the app) so the UI always reads on the main
thread.
"""

from __future__ import annotations

import json
import threading
import time
from enum import Enum
from http import HTTPStatus
from typing import Callable, Dict, List, Optional

from .. import b64, crypto, log
from ..entry import AuthEntry
from ..search import fold
from ..store.cache import CachedEntity, EntityCache, Snapshot
from ..store.keyring import Keyring, MemoryKeyring
from .api import APIError, EnteAPI
from .keyunwrap import unwrap
from .models import AuthenticatorKey, AuthEntityDiff, AuthorizationResponse

__all__ = ["EnteVault", "VaultState", "VaultError"]


class VaultState(Enum):
    SIGNED_OUT = "signedOut"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


class VaultError(Exception):
    NO_AUTHENTICATOR_DATA = ("This Ente account has no authenticator data yet. "
                             "Add codes in the Ente Auth app, then sign in again.")


class _Keys:
    token = "ente.token"
    auth_key = "ente.authKey"
    email = "ente.email"


class EnteVault:
    def __init__(self, api: EnteAPI, keyring: Optional[Keyring] = None, cache: Optional[EntityCache] = None,
                 dispatch: Callable[[Callable[[], None]], object] = lambda fn: fn()):
        self._api = api
        self._keyring: Keyring = keyring if keyring is not None else MemoryKeyring()
        self._cache = cache or EntityCache()
        self._dispatch = dispatch
        self._observers: List[Callable[[], None]] = []
        self._refresh_lock = threading.Lock()
        self._refresh_done: Optional[threading.Event] = None
        self._refresh_generation = 0
        # Session changes invalidate worker results before they can publish or save.
        self._session_lock = threading.RLock()
        self._generation = 0
        self._login_pending = False
        self._login_lock = threading.Lock()

        self.entries: List[AuthEntry] = []
        self.state: VaultState = VaultState.SIGNED_OUT
        self.error_message: str = ""
        self.account_email: Optional[str] = None
        self.last_sync: Optional[float] = None
        #: True when the persisted token was rejected (HTTP 401) — the user must sign in
        #: again before anything will sync. Cached entries keep working offline meanwhile.
        self.session_expired = False

        #: The unwrapped 32-byte authenticator key (in memory while signed in).
        self._auth_key: Optional[bytes] = None

    # MARK: Observers

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        def fire() -> None:
            for callback in list(self._observers):
                try:
                    callback()
                except Exception:
                    log.ente.exception("Vault observer failed")
            return False  # so GLib.idle_add doesn't repeat
        self._dispatch(fire)

    def _set_state(self, state: VaultState, message: str = "") -> None:
        self.state = state
        self.error_message = message
        self._notify()

    def adopt_keyring(self, keyring: Keyring) -> None:
        """Swaps in the real keyring once it has been opened. Opening the Secret Service
        can block on a desktop prompt (unlock / create keyring), so the app resolves it on
        a worker thread and hands it over here before restoring the session.

        A login completed meanwhile lives in the interim (memory) keyring and is the
        freshest session there is: it moves into the adopted keyring rather than being
        thrown away. Should the adopted keyring refuse the writes, the interim one stays
        in use (memory-only, which the UI flags) instead of losing the session."""
        with self._session_lock:
            interim = self._keyring
            adopted = interim is keyring or not self._has_session(interim) or self._copy_session(interim, keyring)
            if adopted:
                self._keyring = keyring
        if not adopted:
            log.ente.warning("Couldn't move the session into the keyring; keeping it in memory")
        self._notify()

    @staticmethod
    def _has_session(keyring: Keyring) -> bool:
        return keyring.get(_Keys.token) is not None and keyring.get(_Keys.auth_key) is not None

    @staticmethod
    def _copy_session(source: Keyring, target: Keyring) -> bool:
        """Copies the session items into ``target``; on a failed write, rolls back what
        was written so ``target`` never holds half a session."""
        for key in (_Keys.token, _Keys.auth_key, _Keys.email):
            value = source.get(key)
            if value is None:
                continue
            if not target.set(key, value):
                for stale in (_Keys.token, _Keys.auth_key, _Keys.email):
                    target.remove(stale)
                return False
        return True

    @property
    def keyring_persistent(self) -> bool:
        return bool(getattr(self._keyring, "persistent", False))

    @property
    def is_signed_in(self) -> bool:
        return self._has_session(self._keyring)

    # MARK: Session restore

    def restore(self) -> None:
        """Restores a persisted session: load token + authKey, decrypt cached entities, then
        kick off a network refresh. Call once at launch."""
        with self._session_lock:
            if self._login_pending:
                return
            self._generation += 1
            token = self._keyring.get(_Keys.token)
            auth_key = self._keyring.get(_Keys.auth_key)
            if token is None or auth_key is None:
                self._auth_key = None
                self.account_email = None
                self.entries = []
                self.last_sync = None
                self.session_expired = False
                self._api.set_auth_token(None)
                self._set_state(VaultState.SIGNED_OUT)
                return
            self._api.set_auth_token(token.decode("utf-8", "replace"))
            self._auth_key = bytes(auth_key)
            email = self._keyring.get(_Keys.email)
            self.account_email = email.decode("utf-8", "replace") if email else None

            # Publish the offline cache before starting network work.
            snapshot = self._cache.load()
            self.entries = self._decrypt(snapshot.entities)
            self._set_state(VaultState.LOADING if not self.entries else VaultState.READY)

        self.refresh()

    # MARK: Login

    def complete_login(self, authorization: AuthorizationResponse, password: str, email: str) -> None:
        """Completes login from an authorized response: unwrap keys, fetch + unwrap the
        authenticator key, persist the session, and do a first sync."""
        with self._session_lock:
            generation = self._generation
        with self._login_lock:
            self._complete_login(authorization, password, email, generation)

    def _complete_login(self, authorization: AuthorizationResponse, password: str, email: str,
                        expected_generation: int) -> None:
        with self._session_lock:
            # A queued login must honor sign-out while it waited for the active login.
            self._check_generation(expected_generation)
            self._generation += 1
            generation = self._generation
            self._login_pending = True
            self._set_state(VaultState.LOADING)
        try:
            keys = unwrap(authorization, password)
            with self._session_lock:
                self._check_generation(generation)
                self._api.set_auth_token(keys.token)

            try:
                wrapped = AuthenticatorKey.from_json(self._api.get("authenticator/key", authenticated=True))
            except APIError as error:
                if error.kind == "http" and error.status == HTTPStatus.NOT_FOUND:
                    # `/authenticator/key` 404s for accounts that have never used Ente Auth —
                    # the generic "account may not exist" message would be wrong and confusing.
                    raise VaultError(VaultError.NO_AUTHENTICATOR_DATA) from None
                raise
            encrypted = b64.decode_standard(wrapped.encrypted_key)
            nonce = b64.decode_standard(wrapped.header)
            if encrypted is None or nonce is None:
                raise crypto.CryptoError("bad_length", "authenticator key")
            unwrapped_auth_key = crypto.secret_box_open(encrypted, nonce, keys.master_key)

            with self._session_lock:
                self._check_generation(generation)
                self._persist_session(keys.token, unwrapped_auth_key, email)
                self._auth_key = unwrapped_auth_key
                self.account_email = email
                self.entries = []
                self.last_sync = None
                self.session_expired = False
                self._cache.clear()
                self._login_pending = False
            self.refresh()
            with self._session_lock:
                self._check_generation(generation)
        except Exception:
            with self._session_lock:
                # A late failure must not restore a session the user signed out of.
                if generation == self._generation:
                    self._login_pending = False
                    self.state = VaultState.SIGNED_OUT if not self.entries else VaultState.READY
                    stale = self._keyring.get(_Keys.token)
                    self._api.set_auth_token(stale.decode("utf-8", "replace") if stale else None)
                    self._notify()
            raise

    def _check_generation(self, generation: int) -> None:
        if generation != self._generation:
            raise InterruptedError("Session changed during login")

    def _persist_session(self, token: str, auth_key: bytes, email: str) -> None:
        # Stage a complete session so failed keyring writes can fall back to memory.
        session = MemoryKeyring()
        session.set(_Keys.token, token.encode("utf-8"))
        session.set(_Keys.auth_key, auth_key)
        session.set(_Keys.email, email.encode("utf-8"))
        if self._copy_session(session, self._keyring):
            return
        self._keyring = session
        log.ente.warning("Couldn't save the session to the keyring; keeping it in memory")

    # MARK: Sync

    def refresh(self) -> None:
        """Fetches new/changed entities since the cached cursor, updates the cache, and
        republishes the decrypted entries. Concurrent callers (menu + launch + timer)
        coalesce into one network pass instead of racing the cache."""
        while True:
            with self._session_lock:
                generation = self._generation
            with self._refresh_lock:
                in_flight = self._refresh_done
                joined_generation = self._refresh_generation
                if in_flight is None:
                    done = threading.Event()
                    self._refresh_done = done
                    self._refresh_generation = generation
                    break
            in_flight.wait()
            if joined_generation == generation:
                return
            # A new login needs its own pass, not the obsolete pass it waited for.
        try:
            self._perform_refresh(generation)
        finally:
            with self._refresh_lock:
                self._refresh_done = None
            done.set()

    def _perform_refresh(self, generation: int) -> None:
        try:
            with self._session_lock:
                if generation != self._generation or self._auth_key is None or self._login_pending:
                    return
                if not self.entries:
                    self._set_state(VaultState.LOADING)
                snapshot = self._cache.load()

            by_id: Dict[str, CachedEntity] = {entity.id: entity for entity in snapshot.entities}
            cursor = snapshot.since_time
            limit = 500

            while True:
                cursor_at_page_start = cursor
                diff = AuthEntityDiff.from_json(self._api.get(
                    "authenticator/entity/diff",
                    [("sinceTime", str(cursor)), ("limit", str(limit))],
                    authenticated=True)).diff
                with self._session_lock:
                    if generation != self._generation:
                        return
                for entity in diff:
                    if entity.updated_at is not None:
                        cursor = max(cursor, entity.updated_at)
                    if entity.is_deleted:
                        by_id.pop(entity.id, None)
                    elif entity.encrypted_data is not None and entity.header is not None:
                        by_id[entity.id] = CachedEntity(entity.id, entity.encrypted_data, entity.header)
                # Stop on a short page, or if a full page failed to advance the cursor
                # (otherwise we'd request the same window forever).
                if len(diff) < limit or cursor == cursor_at_page_start:
                    break

            with self._session_lock:
                if generation != self._generation:
                    return
                snapshot = Snapshot(list(by_id.values()), cursor)
                self._cache.save(snapshot)
                self.entries = self._decrypt(snapshot.entities)
                self.last_sync = time.time()
                self.session_expired = False
                self._set_state(VaultState.READY)
        except Exception as error:
            with self._session_lock:
                if generation != self._generation:
                    return
                self._refresh_failed(error)

    def _refresh_failed(self, error: Exception) -> None:
        # Keep offline codes on transient failures; a dead token needs sign-in.
        if isinstance(error, APIError) and error.kind == "http" and error.status == HTTPStatus.UNAUTHORIZED:
            self.session_expired = True
            self._set_state(VaultState.ERROR, "Session expired — please sign in again.")
        elif not self.entries:
            self._set_state(VaultState.ERROR, str(error))
        else:
            self._notify()
        log.ente.error("Refresh failed: %s", error)

    # MARK: Sign out

    def sign_out(self) -> None:
        with self._session_lock:
            self._generation += 1
            self._login_pending = False
            self._keyring.remove(_Keys.token)
            self._keyring.remove(_Keys.auth_key)
            self._keyring.remove(_Keys.email)
            self._cache.clear()
            self._auth_key = None
            self.account_email = None
            self.entries = []
            self.session_expired = False
            self.last_sync = None
            self._api.set_auth_token(None)
            self._set_state(VaultState.SIGNED_OUT)

    # MARK: Decryption

    def _decrypt(self, entities: List[CachedEntity]) -> List[AuthEntry]:
        """Decrypts cached entities into displayable entries, sorted by display name.
        Entries that fail to decrypt or parse are skipped (logged), so one bad row can't
        break the list."""
        auth_key = self._auth_key
        if auth_key is None:
            return []
        result: List[AuthEntry] = []
        for entity in entities:
            cipher = b64.decode_standard(entity.encrypted_data)
            header = b64.decode_standard(entity.header)
            if cipher is None or header is None:
                log.ente.error("Skipped a cached entity with malformed base64: %s", entity.id)
                continue
            try:
                plaintext = crypto.secret_stream_open_single_chunk(cipher, header, auth_key)
            except crypto.CryptoError as error:
                log.ente.error("Skipped an entity that failed to decrypt: %s", error)
                continue
            uri = self._unwrap_uri(plaintext)
            # Entries in Ente's trash stay in the diff with codeDisplay.trashed set; they
            # must not appear (or type codes) here.
            entry = AuthEntry.parse(uri, entity.id)
            if entry is None:
                log.ente.error("Skipped an entity whose otpauth URI didn't parse: %s", entity.id)
            elif not entry.is_trashed:
                result.append(entry)
        result.sort(key=lambda entry: fold(entry.display_name))
        return result

    @staticmethod
    def _unwrap_uri(plaintext: bytes) -> str:
        """The decrypted entity payload is a JSON string literal wrapping the ``otpauth://``
        URI (e.g. ``"otpauth://totp/…"``). Unwrap it, tolerating a bare (unquoted) URI too."""
        text = plaintext.decode("utf-8", "replace")
        try:
            decoded = json.loads(text)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
        return text

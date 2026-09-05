"""End-to-end key hierarchy + sync against a fake Ente API: keys are wrapped exactly as
Ente's server stores them (Argon2id KEK → secretbox master key → secretbox secret key →
sealed-box token → secretbox authenticator key → secretstream entities)."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import nacl.bindings as sodium
from nacl.public import PrivateKey, SealedBox
from nacl.secret import SecretBox

from gans import b64, crypto
from gans.ente.api import APIError, EnteAPI
from gans.ente.keyunwrap import UnwrapError, unwrap
from gans.ente.models import AuthorizationResponse
from gans.ente.vault import EnteVault, VaultError, VaultState
from gans.store.cache import EntityCache
from gans.store.keyring import MemoryKeyring

PASSWORD = "hunter2 🔑"
OPS, MEM = 1, 8192  # tiny Argon2 parameters for a fast test (libsodium minimums)


def _fixture():
    kek_salt = bytes(range(16))
    kek = crypto.derive_key_encryption_key(PASSWORD, kek_salt, MEM, OPS)
    master_key = bytes(range(32, 64))
    secret_key_pair = PrivateKey.generate()
    secret_key = bytes(secret_key_pair)
    public_key = bytes(secret_key_pair.public_key)
    auth_key = bytes(range(64, 96))
    token_bytes = bytes(range(100, 132))

    key_nonce = bytes(range(24))
    secret_nonce = bytes(range(1, 25))
    auth_nonce = bytes(range(2, 26))

    encrypted_key = SecretBox(kek).encrypt(master_key, key_nonce).ciphertext
    encrypted_secret_key = SecretBox(master_key).encrypt(secret_key, secret_nonce).ciphertext
    encrypted_token = SealedBox(secret_key_pair.public_key).encrypt(token_bytes)
    encrypted_auth_key = SecretBox(master_key).encrypt(auth_key, auth_nonce).ciphertext

    authorization = {
        "id": 42,
        "keyAttributes": {
            "kekSalt": b64.encode_standard(kek_salt),
            "encryptedKey": b64.encode_standard(encrypted_key),
            "keyDecryptionNonce": b64.encode_standard(key_nonce),
            "publicKey": b64.encode_standard(public_key),
            "encryptedSecretKey": b64.encode_standard(encrypted_secret_key),
            "secretKeyDecryptionNonce": b64.encode_standard(secret_nonce),
            "memLimit": MEM,
            "opsLimit": OPS,
        },
        "encryptedToken": b64.encode_standard(encrypted_token),
    }
    return {
        "authorization": authorization,
        "token": b64.encode_url_safe_padded(token_bytes),
        "master_key": master_key,
        "secret_key": secret_key,
        "auth_key": auth_key,
        "authenticator_key": {"userID": 42, "encryptedKey": b64.encode_standard(encrypted_auth_key),
                              "header": b64.encode_standard(auth_nonce)},
    }


def _entity(auth_key, entity_id, uri, updated_at, deleted=False, quoted=True):
    if deleted:
        return {"id": entity_id, "isDeleted": True, "updatedAt": updated_at}
    payload = json.dumps(uri).encode() if quoted else uri.encode()
    state = sodium.crypto_secretstream_xchacha20poly1305_state()
    header = sodium.crypto_secretstream_xchacha20poly1305_init_push(state, auth_key)
    cipher = sodium.crypto_secretstream_xchacha20poly1305_push(state, payload, None, 0)
    return {"id": entity_id, "encryptedData": b64.encode_standard(cipher), "header": b64.encode_standard(header),
            "isDeleted": False, "createdAt": updated_at, "updatedAt": updated_at}


class FakeAPI(EnteAPI):
    """Serves the fixture from memory; records tokens seen on authenticated requests."""

    def __init__(self, fixture):
        super().__init__("https://fake.invalid")
        self.fixture = fixture
        self.pages = []          # list of diff pages returned in order of sinceTime requests
        self.seen_tokens = []
        self.fail_with = None    # APIError to raise on the next call (authenticated or not)
        self.calls = []
        self.hold_diff = None    # an Event: diff requests block until it is set

    def _send(self, method, path, query, body, authenticated):
        self.calls.append((method, path, dict(query)))
        if authenticated:
            self.seen_tokens.append(self._auth_token)
        if self.fail_with is not None:
            error, self.fail_with = self.fail_with, None
            raise error
        if path == "authenticator/key":
            return self.fixture["authenticator_key"]
        if path == "authenticator/entity/diff":
            if self.hold_diff is not None:
                self.hold_diff.wait(10)
            since = int(dict(query)["sinceTime"])
            for page_since, page in self.pages:
                if page_since == since:
                    return {"diff": page}
            return {"diff": []}
        raise AssertionError(f"unexpected request {method} {path}")


class KeyUnwrapTests(unittest.TestCase):
    def test_unwraps_keys_and_token(self):
        fixture = _fixture()
        keys = unwrap(AuthorizationResponse.from_json(fixture["authorization"]), PASSWORD)
        self.assertEqual(keys.master_key, fixture["master_key"])
        self.assertEqual(keys.secret_key, fixture["secret_key"])
        self.assertEqual(keys.token, fixture["token"])
        self.assertTrue(keys.token.endswith("="))  # 32 token bytes → one '=' of padding, preserved

    def test_wrong_password_fails_cleanly(self):
        fixture = _fixture()
        with self.assertRaises(crypto.CryptoError) as context:
            unwrap(AuthorizationResponse.from_json(fixture["authorization"]), "wrong")
        self.assertEqual(context.exception.kind, "operation_failed")

    def test_plain_token_fallback_and_missing_token(self):
        fixture = _fixture()
        auth = dict(fixture["authorization"])
        del auth["encryptedToken"]
        auth["token"] = "plain-token"
        self.assertEqual(unwrap(AuthorizationResponse.from_json(auth), PASSWORD).token, "plain-token")
        del auth["token"]
        with self.assertRaises(UnwrapError):
            unwrap(AuthorizationResponse.from_json(auth), PASSWORD)

    def test_missing_key_attributes_and_bad_fields(self):
        with self.assertRaises(UnwrapError):
            unwrap(AuthorizationResponse.from_json({"encryptedToken": "x"}), PASSWORD)
        fixture = _fixture()
        auth = json.loads(json.dumps(fixture["authorization"]))
        auth["keyAttributes"]["kekSalt"] = "!!!"
        with self.assertRaises(UnwrapError) as context:
            unwrap(AuthorizationResponse.from_json(auth), PASSWORD)
        self.assertIn("kekSalt", str(context.exception))


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _fixture()
        self.api = FakeAPI(self.fixture)
        self.keyring = MemoryKeyring()
        self._dir = tempfile.TemporaryDirectory()
        self.cache = EntityCache(Path(self._dir.name) / "entities.json")
        self.changes = []
        self.vault = EnteVault(self.api, self.keyring, self.cache, dispatch=lambda fn: fn())
        self.vault.on_change(lambda: self.changes.append(self.vault.state))
        auth_key = self.fixture["auth_key"]
        self.api.pages = [(0, [
            _entity(auth_key, "e1", "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP&issuer=GitHub", 1000),
            _entity(auth_key, "e2", "otpauth://totp/Amazon:bob?secret=JBSWY3DPEHPK3PXP", 2000, quoted=False),
            _entity(auth_key, "e3", "otpauth://totp/Trash:x?secret=JBSWY3DPEHPK3PXP&codeDisplay=%7B%22trashed%22%3Atrue%7D", 3000),
            {"id": "e4", "encryptedData": "!!!", "header": "!!!", "isDeleted": False, "updatedAt": 3500},
        ])]

    def tearDown(self):
        self._dir.cleanup()

    def _login(self):
        self.vault.complete_login(AuthorizationResponse.from_json(self.fixture["authorization"]), PASSWORD, "alice@example.com")

    def test_login_persists_session_and_syncs(self):
        self.assertFalse(self.vault.is_signed_in)
        self._login()
        self.assertTrue(self.vault.is_signed_in)
        self.assertEqual(self.keyring.get("ente.token"), self.fixture["token"].encode())
        self.assertEqual(self.keyring.get("ente.authKey"), self.fixture["auth_key"])
        self.assertEqual(self.keyring.get("ente.email"), b"alice@example.com")
        self.assertEqual(self.api.seen_tokens[-1], self.fixture["token"])
        self.assertIs(self.vault.state, VaultState.READY)
        # Sorted by name; the trashed entry and the undecryptable row are skipped.
        self.assertEqual([e.issuer for e in self.vault.entries], ["Amazon", "GitHub"])
        self.assertEqual(self.vault.account_email, "alice@example.com")
        self.assertIsNotNone(self.vault.last_sync)
        self.assertEqual(self.cache.load().since_time, 3500)
        self.assertIn(VaultState.LOADING, self.changes)

    def test_failed_keyring_writes_keep_a_complete_memory_session(self):
        # Losing the keyring mid-login must not leave a token paired with an old key.
        for failed_key in ("ente.token", "ente.authKey", "ente.email"):
            with self.subTest(failed_key=failed_key):
                self.keyring.set("ente.token", b"old-token")
                self.keyring.set("ente.authKey", bytes(32))
                self.keyring.set("ente.email", b"old@example.com")
                self.vault = EnteVault(self.api, self.keyring, self.cache)
                original_set = self.keyring.set

                def write(account, data):
                    return False if account == failed_key else original_set(account, data)

                with patch.object(self.keyring, "persistent", True), patch.object(self.keyring, "set", side_effect=write):
                    self._login()
                    self.assertTrue(self.vault.is_signed_in)
                    self.assertFalse(self.vault.keyring_persistent)

                for key in ("ente.token", "ente.authKey", "ente.email"):
                    self.assertIsNone(self.keyring.get(key))
                self.vault.restore()
                self.assertEqual(self.vault.account_email, "alice@example.com")
                self.assertEqual([e.issuer for e in self.vault.entries], ["Amazon", "GitHub"])
                self.assertEqual(self.api._auth_token, self.fixture["token"])

    def test_restore_decrypts_cache_then_refreshes_with_diff(self):
        self._login()
        # A second vault instance (a relaunch) restores from keyring + cache, then applies a
        # diff: e1 deleted, e5 added, using the persisted cursor.
        auth_key = self.fixture["auth_key"]
        self.api.pages = [(3500, [
            _entity(auth_key, "e1", "", 4000, deleted=True),
            _entity(auth_key, "e5", "otpauth://totp/Zulu:z?secret=JBSWY3DPEHPK3PXP", 4500),
        ])]
        relaunched = EnteVault(self.api, self.keyring, self.cache, dispatch=lambda fn: fn())
        relaunched.restore()
        self.assertIs(relaunched.state, VaultState.READY)
        self.assertEqual([e.issuer for e in relaunched.entries], ["Amazon", "Zulu"])
        self.assertEqual(self.cache.load().since_time, 4500)
        self.assertEqual(relaunched.account_email, "alice@example.com")

    def test_keyring_can_be_adopted_after_construction(self):
        # The app opens the Secret Service on a worker thread (it may prompt) and hands it
        # over later; until then the vault must simply look signed out.
        late = EnteVault(self.api, None, self.cache, dispatch=lambda fn: fn())
        self.assertFalse(late.is_signed_in)
        self.assertFalse(late.keyring_persistent)
        self._login()  # populates self.keyring
        changes = []
        late.on_change(lambda: changes.append(1))
        late.adopt_keyring(self.keyring)
        self.assertTrue(late.is_signed_in)
        self.assertEqual(changes, [1])
        late.restore()
        self.assertEqual([e.issuer for e in late.entries], ["Amazon", "GitHub"])

    def test_adopting_a_keyring_carries_over_a_login_completed_meanwhile(self):
        # The user signs in while the Secret Service prompt is still pending: the session
        # sits in the interim memory keyring and must survive the swap to the real one
        # (which may hold an older session of its own — the fresh login wins).
        late = EnteVault(self.api, None, self.cache, dispatch=lambda fn: fn())
        late.complete_login(AuthorizationResponse.from_json(self.fixture["authorization"]), PASSWORD, "alice@example.com")
        real = MemoryKeyring()
        real.set("ente.token", b"old-token")
        real.set("ente.authKey", bytes(32))
        late.adopt_keyring(real)
        self.assertTrue(late.is_signed_in)
        self.assertEqual(real.get("ente.token"), self.fixture["token"].encode())
        self.assertEqual(real.get("ente.authKey"), self.fixture["auth_key"])
        self.assertEqual(real.get("ente.email"), b"alice@example.com")
        # The app restores right after adopting: still the same session, nothing lost.
        late.restore()
        self.assertIs(late.state, VaultState.READY)
        self.assertEqual([e.issuer for e in late.entries], ["Amazon", "GitHub"])
        self.assertEqual(late.account_email, "alice@example.com")

    def test_adopting_a_keyring_that_rejects_the_session_keeps_it_in_memory(self):
        class RejectingKeyring(MemoryKeyring):
            persistent = True

            def set(self, account, data):
                if account == "ente.authKey":
                    return False
                return super().set(account, data)

        late = EnteVault(self.api, None, self.cache, dispatch=lambda fn: fn())
        late.complete_login(AuthorizationResponse.from_json(self.fixture["authorization"]), PASSWORD, "alice@example.com")
        rejecting = RejectingKeyring()
        late.adopt_keyring(rejecting)
        self.assertTrue(late.is_signed_in)
        self.assertFalse(late.keyring_persistent)                # the interim keyring stays in use
        self.assertIsNone(rejecting.get("ente.token"))           # no half-written session left behind
        late.restore()
        self.assertEqual([e.issuer for e in late.entries], ["Amazon", "GitHub"])

    def test_adopting_a_keyring_without_an_interim_login_just_swaps(self):
        late = EnteVault(self.api, None, self.cache, dispatch=lambda fn: fn())
        real = MemoryKeyring()
        late.adopt_keyring(real)
        self.assertFalse(late.is_signed_in)
        self.assertIsNone(real.get("ente.token"))

    def test_restore_without_session_is_signed_out(self):
        self.vault.restore()
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertEqual(self.api.calls, [])

    def test_restore_without_session_clears_a_stale_in_memory_session(self):
        # The keyring items vanished (or a keyring without the session was adopted): the
        # entries and key of the earlier session must go too, not linger as SIGNED_OUT + codes.
        self._login()
        self.keyring.remove("ente.token")
        self.vault.restore()
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertEqual(self.vault.entries, [])
        self.assertIsNone(self.vault.account_email)
        self.assertIsNone(self.vault._auth_key)
        self.assertIsNone(self.api._auth_token)
        calls_before = len(self.api.calls)
        self.vault.refresh()
        self.assertEqual(len(self.api.calls), calls_before)      # nothing to sync with

    def test_pagination_stops_on_short_page_and_stuck_cursor(self):
        self._login()
        auth_key = self.fixture["auth_key"]
        full_page = [_entity(auth_key, f"p{i}", f"otpauth://totp/P{i}:x?secret=JBSWY3DPEHPK3PXP", 5000) for i in range(500)]
        # A full page that doesn't advance the cursor (all updatedAt == 5000) must not loop forever.
        self.api.pages = [(3500, full_page), (5000, full_page)]
        self.vault.refresh()
        self.assertEqual(len([c for c in self.api.calls if c[1] == "authenticator/entity/diff"]), 3)  # login + 2 pages
        self.assertEqual(len(self.vault.entries), 502)

    def test_expired_token_flags_session(self):
        self._login()
        self.api.fail_with = APIError("http", status=401, body="expired")
        self.vault.refresh()
        self.assertTrue(self.vault.session_expired)
        self.assertIs(self.vault.state, VaultState.ERROR)
        self.assertEqual(len(self.vault.entries), 2)  # cached entries keep working
        self.assertTrue(self.vault.is_signed_in)

    def test_transient_error_keeps_entries(self):
        self._login()
        self.api.fail_with = APIError("transport", message="offline")
        self.vault.refresh()
        self.assertFalse(self.vault.session_expired)
        self.assertIs(self.vault.state, VaultState.READY)
        self.assertEqual(len(self.vault.entries), 2)

    def test_refresh_reports_a_cache_read_failure_without_raising(self):
        self._login()
        with patch.object(self.cache, "load", side_effect=ValueError("Malformed cache")), \
             self.assertLogs("gans.ente", level="ERROR"):
            self.vault.refresh()
        self.assertIs(self.vault.state, VaultState.READY)
        self.assertEqual(len(self.vault.entries), 2)

    def test_login_with_no_authenticator_data(self):
        self.api.fail_with = APIError("http", status=404)
        with self.assertRaises(VaultError) as context:
            self._login()
        self.assertEqual(str(context.exception), VaultError.NO_AUTHENTICATOR_DATA)
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertFalse(self.vault.is_signed_in)
        self.assertIsNone(self.api._auth_token)

    def test_login_with_wrong_password_leaves_vault_signed_out(self):
        with self.assertRaises(crypto.CryptoError):
            self.vault.complete_login(AuthorizationResponse.from_json(self.fixture["authorization"]), "nope", "a@b")
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)

    def test_sign_out_clears_everything(self):
        self._login()
        self.vault.sign_out()
        self.assertFalse(self.vault.is_signed_in)
        self.assertEqual(self.vault.entries, [])
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertEqual(self.cache.load().entities, [])
        self.assertIsNone(self.vault.account_email)

    def test_sign_out_discards_an_in_flight_refresh(self):
        self._login()
        entered, release = threading.Event(), threading.Event()
        original_send = self.api._send

        def delayed_send(*args):
            response = original_send(*args)
            entered.set()
            self.assertTrue(release.wait(5))
            return response

        with patch.object(self.api, "_send", side_effect=delayed_send):
            worker = threading.Thread(target=self.vault.refresh)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.vault.sign_out()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertEqual(self.cache.load().entities, [])
        self.assertEqual(self.cache.load().since_time, 0)
        self.assertEqual(self.vault.entries, [])
        self.assertIsNone(self.vault.last_sync)
        self.assertFalse(self.vault.is_signed_in)

    def test_sign_out_discards_an_in_flight_refresh_error(self):
        self._login()
        entered, release = threading.Event(), threading.Event()

        def delayed_error(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            raise APIError("http", status=401)

        with patch.object(self.api, "_send", side_effect=delayed_error):
            worker = threading.Thread(target=self.vault.refresh)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.vault.sign_out()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertFalse(self.vault.session_expired)
        self.assertEqual(self.vault.error_message, "")

    def test_sign_out_discards_an_in_flight_login(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        original_unwrap = unwrap

        def delayed_unwrap(*args):
            result = original_unwrap(*args)
            entered.set()
            self.assertTrue(release.wait(5))
            return result

        def login():
            try:
                self._login()
            except InterruptedError as error:
                errors.append(error)

        with patch("gans.ente.vault.unwrap", side_effect=delayed_unwrap):
            worker = threading.Thread(target=login)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.vault.sign_out()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.vault.is_signed_in)
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertIsNone(self.api._auth_token)
        self.assertEqual(self.cache.load().entities, [])

    def test_sign_out_cancels_a_login_waiting_for_another_login(self):
        entered = threading.Event()
        gate = threading.Lock()
        gate.acquire()
        errors = []

        class WaitingLock:
            def __enter__(self):
                entered.set()
                gate.acquire()

            def __exit__(self, *exc):
                gate.release()

        def login():
            try:
                self._login()
            except InterruptedError as error:
                errors.append(error)

        with patch.object(self.vault, "_login_lock", WaitingLock()):
            worker = threading.Thread(target=login)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.vault.sign_out()
            finally:
                gate.release()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.vault.is_signed_in)
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)

    def test_new_login_gets_its_own_sync_after_an_obsolete_refresh(self):
        self._login()
        entered, release = threading.Event(), threading.Event()
        original_send = self.api._send
        old_worker = None
        new_worker = None
        original_persist = self.vault._persist_session
        persisted = threading.Event()

        def delayed_send(*args):
            response = original_send(*args)
            if threading.current_thread() is old_worker:
                entered.set()
                self.assertTrue(release.wait(5))
            return response

        def persist(*args):
            original_persist(*args)
            persisted.set()

        with patch.object(self.api, "_send", side_effect=delayed_send), patch.object(
            self.vault, "_persist_session", side_effect=persist
        ):
            old_worker = threading.Thread(target=self.vault.refresh)
            old_worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.api.pages = [(0, [_entity(self.fixture["auth_key"], "new",
                    "otpauth://totp/New?secret=JBSWY3DPEHPK3PXP", 100)])]
                new_worker = threading.Thread(target=self._login)
                new_worker.start()
                self.assertTrue(persisted.wait(5))
            finally:
                release.set()
                old_worker.join(5)
                if new_worker is not None:
                    new_worker.join(5)
        self.assertFalse(old_worker.is_alive())
        self.assertFalse(new_worker.is_alive())
        self.assertEqual([entry.id for entry in self.vault.entries], ["new"])
        self.assertEqual(self.cache.load().since_time, 100)
        self.assertIs(self.vault.state, VaultState.READY)

    def test_refresh_during_login_does_not_register_a_pass(self):
        self._login()
        entered, release = threading.Event(), threading.Event()

        def delayed_unwrap(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return unwrap(*args)

        with patch("gans.ente.vault.unwrap", side_effect=delayed_unwrap):
            worker = threading.Thread(target=self._login)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with patch.object(self.vault, "_perform_refresh", wraps=self.vault._perform_refresh) as perform:
                    self.vault.refresh()
                    perform.assert_not_called()
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIs(self.vault.state, VaultState.READY)
        self.assertEqual(len(self.vault.entries), 2)

    def test_concurrent_refreshes_coalesce(self):
        self._login()
        before = len(self.api.calls)
        # Hold the first diff request open until every thread has entered refresh(): a
        # correct vault makes the others wait for that one pass (one request in total);
        # without coalescing each thread would issue its own.
        self.api.hold_diff = threading.Event()
        entered = threading.Semaphore(0)

        class CountingLock:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self):
                self._inner.acquire()
                entered.release()

            def __exit__(self, *exc):
                self._inner.release()

        self.vault._refresh_lock = CountingLock(self.vault._refresh_lock)
        finished = []
        threads = [threading.Thread(target=lambda: (self.vault.refresh(), finished.append(time.monotonic())))
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 5
        for _ in threads:
            entered.acquire(timeout=max(0.0, deadline - time.monotonic()))
        released_at = time.monotonic()
        self.api.hold_diff.set()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(finished), 5)
        self.assertTrue(all(at >= released_at for at in finished))  # everyone waited for the in-flight pass
        diff_calls = [c for c in self.api.calls[before:] if c[1] == "authenticator/entity/diff"]
        self.assertEqual(len(diff_calls), 1)
        self.assertEqual([c for c in self.api.calls[before:] if c[1] != "authenticator/entity/diff"], [])
        self.assertIs(self.vault.state, VaultState.READY)
        # Once that pass is over, a new refresh really goes to the network again.
        self.vault.refresh()
        self.assertEqual(len([c for c in self.api.calls[before:] if c[1] == "authenticator/entity/diff"]), 2)


if __name__ == "__main__":
    unittest.main()

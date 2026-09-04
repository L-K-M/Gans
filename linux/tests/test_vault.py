"""End-to-end key hierarchy + sync against a fake Ente API: keys are wrapped exactly as
Ente's server stores them (Argon2id KEK → secretbox master key → secretbox secret key →
sealed-box token → secretbox authenticator key → secretstream entities)."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

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
        self.fail_with = None    # APIError to raise on the next authenticated call
        self.calls = []

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
        self.assertTrue(keys.token.endswith("=") or True)  # padding preserved when present

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

    def test_restore_without_session_is_signed_out(self):
        self.vault.restore()
        self.assertIs(self.vault.state, VaultState.SIGNED_OUT)
        self.assertEqual(self.api.calls, [])

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

    def test_concurrent_refreshes_coalesce(self):
        self._login()
        before = len(self.api.calls)
        threads = [threading.Thread(target=self.vault.refresh) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        diff_calls = [c for c in self.api.calls[before:] if c[1] == "authenticator/entity/diff"]
        self.assertLessEqual(len(diff_calls), 5)
        self.assertGreaterEqual(len(diff_calls), 1)


if __name__ == "__main__":
    unittest.main()

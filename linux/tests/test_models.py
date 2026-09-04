import unittest

from gans.ente.models import (AuthEntityDiff, AuthorizationResponse, CreateSRPSessionResponse, DecodingError,
                              KeyAttributes, SRPAttributes)


class ModelTests(unittest.TestCase):
    def test_srp_attributes(self):
        attributes = SRPAttributes.from_json({"attributes": {
            "srpUserID": "u", "srpSalt": "s", "memLimit": 1073741824, "opsLimit": 4, "kekSalt": "k",
            "isEmailMFAEnabled": True}})
        self.assertEqual(attributes.srp_user_id, "u")
        self.assertEqual(attributes.mem_limit, 1073741824)
        self.assertTrue(attributes.is_email_mfa_enabled)
        with self.assertRaises(DecodingError):
            SRPAttributes.from_json({"attributes": {"srpUserID": "u"}})
        with self.assertRaises(DecodingError):
            SRPAttributes.from_json([])

    def test_authorization_response_optional_fields(self):
        auth = AuthorizationResponse.from_json({"twoFactorSessionID": "2fa"})
        self.assertTrue(auth.requires_two_factor)
        self.assertFalse(auth.requires_passkey)
        self.assertIsNone(auth.key_attributes)
        empty = AuthorizationResponse.from_json({"twoFactorSessionID": "", "passkeySessionID": ""})
        self.assertFalse(empty.requires_two_factor)
        self.assertFalse(empty.requires_passkey)
        with_keys = AuthorizationResponse.from_json({"id": 7.0, "keyAttributes": {
            "kekSalt": "a", "encryptedKey": "b", "keyDecryptionNonce": "c", "publicKey": "d",
            "encryptedSecretKey": "e", "secretKeyDecryptionNonce": "f", "memLimit": 8192, "opsLimit": 1},
            "encryptedToken": "t", "accountsUrl": "https://accounts.ente.io", "srpM2": "m2"})
        self.assertEqual(with_keys.id, 7)
        self.assertIsInstance(with_keys.key_attributes, KeyAttributes)
        self.assertEqual(with_keys.accounts_url, "https://accounts.ente.io")

    def test_present_but_malformed_optional_fields_are_decoding_errors(self):
        with self.assertRaises(DecodingError):
            AuthorizationResponse.from_json({"twoFactorSessionID": 12345})
        with self.assertRaises(DecodingError):
            AuthorizationResponse.from_json({"id": True})
        with self.assertRaises(DecodingError):
            AuthEntityDiff.from_json({"diff": [{"id": "1", "isDeleted": False, "createdAt": "yesterday"}]})
        # Absent and null are both simply absent.
        self.assertIsNone(AuthorizationResponse.from_json({"twoFactorSessionID": None}).two_factor_session_id)

    def test_bool_is_not_an_int(self):
        with self.assertRaises(DecodingError):
            KeyAttributes.from_json({"kekSalt": "a", "encryptedKey": "b", "keyDecryptionNonce": "c", "publicKey": "d",
                                     "encryptedSecretKey": "e", "secretKeyDecryptionNonce": "f", "memLimit": True, "opsLimit": 1})

    def test_diff(self):
        diff = AuthEntityDiff.from_json({"diff": [
            {"id": "1", "encryptedData": "x", "header": "h", "isDeleted": False, "updatedAt": 5},
            {"id": "2", "isDeleted": True},
        ]})
        self.assertEqual(len(diff.diff), 2)
        self.assertTrue(diff.diff[1].is_deleted)
        self.assertIsNone(diff.diff[1].encrypted_data)
        self.assertEqual(AuthEntityDiff.from_json({}).diff, [])
        with self.assertRaises(DecodingError):
            AuthEntityDiff.from_json({"diff": "nope"})

    def test_create_srp_session(self):
        response = CreateSRPSessionResponse.from_json({"sessionID": "s", "srpB": "B"})
        self.assertEqual((response.session_id, response.srp_b), ("s", "B"))


if __name__ == "__main__":
    unittest.main()

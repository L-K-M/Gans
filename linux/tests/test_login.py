import unittest
from urllib.parse import parse_qs, urlsplit

from gans.ente.login import EnteLogin


class PasskeyURLTests(unittest.TestCase):
    """The passkey verification URL must match what Ente's accounts page expects, since a
    non-whitelisted ``redirect`` makes the page refuse to run the ceremony."""

    def _components(self, url):
        parts = urlsplit(url)
        return parts, {k: v[0] for k, v in parse_qs(parts.query).items()}

    def test_builds_verification_url(self):
        url = EnteLogin.passkey_verification_url("https://accounts.ente.io", "sess-123", "io.ente.auth")
        parts, items = self._components(url)
        self.assertEqual(parts.hostname, "accounts.ente.io")
        self.assertEqual(parts.path, "/passkeys/verify")
        self.assertEqual(items["passkeySessionID"], "sess-123")
        self.assertEqual(items["clientPackage"], "io.ente.auth")
        self.assertEqual(items["redirect"], "ente-cli://passkey")

    def test_falls_back_to_default_accounts_host(self):
        parts, _ = self._components(EnteLogin.passkey_verification_url("", "s", "io.ente.auth"))
        self.assertEqual(parts.hostname, "accounts.ente.io")

    def test_rejects_non_ente_accounts_host(self):
        parts, _ = self._components(EnteLogin.passkey_verification_url("https://evil.example.com", "s", "io.ente.auth"))
        self.assertEqual(parts.hostname, "accounts.ente.io")

    def test_rejects_lookalike_ente_host(self):
        self.assertEqual(EnteLogin.sanitized_accounts_base("https://accounts.ente.io.evil.com"), EnteLogin.DEFAULT_ACCOUNTS_URL)
        self.assertEqual(EnteLogin.sanitized_accounts_base("https://evilente.io"), EnteLogin.DEFAULT_ACCOUNTS_URL)

    def test_rejects_non_https_accounts_url(self):
        parts, _ = self._components(EnteLogin.passkey_verification_url("http://accounts.ente.io", "s", "io.ente.auth"))
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.hostname, "accounts.ente.io")

    def test_keeps_legitimate_ente_subdomain(self):
        self.assertEqual(EnteLogin.sanitized_accounts_base("https://accounts.ente.io"), "https://accounts.ente.io")

    def test_userinfo_trick_is_rejected(self):
        # "https://accounts.ente.io@evil.com" has host evil.com, not ente.io.
        self.assertEqual(EnteLogin.sanitized_accounts_base("https://accounts.ente.io@evil.com"), EnteLogin.DEFAULT_ACCOUNTS_URL)


if __name__ == "__main__":
    unittest.main()

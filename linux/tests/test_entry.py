import unittest

from gans import base32
from gans.entry import AuthEntry
from gans.otp import OTPAlgorithm


class OtpAuthURITests(unittest.TestCase):
    def test_parses_standard_totp(self):
        uri = "otpauth://totp/GitHub:alice@example.com?secret=JBSWY3DPEHPK3PXP&issuer=GitHub&algorithm=SHA1&digits=6&period=30"
        entry = AuthEntry.parse(uri, "1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.issuer, "GitHub")
        self.assertEqual(entry.account, "alice@example.com")
        self.assertEqual(entry.digits, 6)
        self.assertEqual(entry.period, 30)
        self.assertIs(entry.algorithm, OTPAlgorithm.SHA1)
        self.assertTrue(entry.kind.is_totp)

    def test_issuer_from_label_prefix_when_query_missing(self):
        entry = AuthEntry.parse("otpauth://totp/AWS:root?secret=JBSWY3DPEHPK3PXP", "2")
        self.assertEqual(entry.issuer, "AWS")
        self.assertEqual(entry.account, "root")

    def test_parses_hotp_with_counter(self):
        entry = AuthEntry.parse("otpauth://hotp/Acme:bob?secret=JBSWY3DPEHPK3PXP&counter=42", "3")
        self.assertTrue(entry.kind.is_hotp)
        self.assertEqual(entry.kind.counter, 42)
        self.assertFalse(entry.is_time_based)

    def test_parses_steam_defaults(self):
        entry = AuthEntry.parse("otpauth://steam/Steam:gamer?secret=JBSWY3DPEHPK3PXP", "4")
        self.assertEqual(entry.digits, 5)
        self.assertTrue(entry.kind.is_steam)
        self.assertEqual(len(entry.code(59)), 5)

    def test_algorithm_falls_back_to_sha1(self):
        self.assertIs(AuthEntry.parse("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&algorithm=BOGUS", "5").algorithm,
                      OTPAlgorithm.SHA1)

    def test_rejects_missing_secret_and_wrong_scheme(self):
        self.assertIsNone(AuthEntry.parse("otpauth://totp/x?issuer=Y", "6"))
        self.assertIsNone(AuthEntry.parse("https://example.com", "7"))
        self.assertIsNone(AuthEntry.parse("", "7b"))
        self.assertIsNone(AuthEntry.parse("otpauth://totp/x?secret=0189", "7c"))

    def test_digits_and_period_clamped_to_safe_range(self):
        entry = AuthEntry.parse("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&digits=12&period=0", "10")
        self.assertEqual(entry.digits, 9)
        self.assertEqual(entry.period, 1)
        self.assertEqual(len(entry.code(59)), 9)

    def test_display_name(self):
        both = AuthEntry.parse("otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP", "8")
        self.assertEqual(both.display_name, "Iss (acct)")
        account_only = AuthEntry.parse("otpauth://totp/justme?secret=JBSWY3DPEHPK3PXP", "9")
        self.assertEqual(account_only.issuer, "")
        self.assertEqual(account_only.account, "justme")
        self.assertEqual(account_only.display_name, "justme")
        issuer_only = AuthEntry.parse("otpauth://totp/?secret=JBSWY3DPEHPK3PXP&issuer=Solo", "9b")
        self.assertEqual(issuer_only.display_name, "Solo")
        neither = AuthEntry.parse("otpauth://totp/?secret=JBSWY3DPEHPK3PXP", "9c")
        self.assertEqual(neither.display_name, "Unknown")

    # MARK: Escaping / robustness

    def test_duplicate_query_keys_first_wins(self):
        entry = AuthEntry.parse("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP&secret=AAAA&digits=6&DIGITS=8", "20")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.digits, 6)
        self.assertEqual(entry.secret, base32.decode("JBSWY3DPEHPK3PXP"))

    def test_label_is_decoded_exactly_once(self):
        entry = AuthEntry.parse("otpauth://totp/Rate%20%2520%20Club:bob?secret=JBSWY3DPEHPK3PXP", "21")
        self.assertEqual(entry.issuer, "Rate %20 Club")
        self.assertEqual(entry.account, "bob")

    def test_encoded_colon_inside_issuer_does_not_split_label(self):
        entry = AuthEntry.parse("otpauth://totp/we%3Aird:alice?secret=JBSWY3DPEHPK3PXP", "22")
        self.assertEqual(entry.issuer, "we:ird")
        self.assertEqual(entry.account, "alice")

    def test_plus_in_query_value_means_space_like_entes_web_client(self):
        self.assertEqual(AuthEntry.parse("otpauth://totp/acct?secret=JBSWY3DPEHPK3PXP&issuer=My+Bank", "23").issuer, "My Bank")
        self.assertEqual(AuthEntry.parse("otpauth://totp/acct?secret=JBSWY3DPEHPK3PXP&issuer=Disney%2B", "24").issuer, "Disney+")

    def test_raw_spaces_in_label_parse_instead_of_being_dropped(self):
        entry = AuthEntry.parse("otpauth://totp/My Bank:alice bob?secret=JBSWY3DPEHPK3PXP", "25")
        self.assertEqual(entry.issuer, "My Bank")
        self.assertEqual(entry.account, "alice bob")

    def test_unicode_names_survive(self):
        entry = AuthEntry.parse("otpauth://totp/B%C3%A4ckerei%20Z%C3%BCrich:fr%C3%A9d%C3%A9ric?secret=JBSWY3DPEHPK3PXP", "26")
        self.assertEqual(entry.issuer, "Bäckerei Zürich")
        self.assertEqual(entry.account, "frédéric")

    def test_uppercase_scheme_and_type(self):
        entry = AuthEntry.parse("OTPAUTH://TOTP/Iss:acct?SECRET=JBSWY3DPEHPK3PXP", "26b")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.kind.is_totp)

    # MARK: codeDisplay metadata

    def test_code_display_trashed_pinned_note(self):
        display = "%7B%22pinned%22%3Atrue%2C%22trashed%22%3Afalse%2C%22note%22%3A%22hello%22%7D"
        entry = AuthEntry.parse(f"otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay={display}", "27")
        self.assertTrue(entry.pinned)
        self.assertFalse(entry.is_trashed)
        self.assertEqual(entry.note, "hello")
        trashed = "%7B%22trashed%22%3Atrue%7D"
        self.assertTrue(AuthEntry.parse(f"otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay={trashed}", "28").is_trashed)

    def test_malformed_code_display_is_ignored(self):
        entry = AuthEntry.parse("otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay=notjson", "29")
        self.assertIsNotNone(entry)
        self.assertFalse(entry.pinned)
        self.assertFalse(entry.is_trashed)

    def test_code_display_tags(self):
        display = "%7B%22tags%22%3A%5B%22work%22%2C%22dev%22%5D%7D"
        entry = AuthEntry.parse(f"otpauth://totp/Iss:acct?secret=JBSWY3DPEHPK3PXP&codeDisplay={display}", "30")
        self.assertEqual(entry.tags, ("work", "dev"))

    # MARK: Codes

    def test_formatted_code_groups_digits(self):
        six = AuthEntry.parse("otpauth://totp/x?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&digits=6", "40")
        self.assertEqual(six.code(59), "287082")
        self.assertEqual(six.formatted_code(59), "287 082")
        eight = AuthEntry.parse("otpauth://totp/x?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&digits=8", "41")
        self.assertEqual(eight.formatted_code(59), "9428 7082")

    def test_fraction_remaining(self):
        entry = AuthEntry.parse("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP", "42")
        self.assertEqual(entry.fraction_remaining(0), 1.0)
        self.assertAlmostEqual(entry.fraction_remaining(15), 0.5)
        self.assertAlmostEqual(entry.precise_fraction_remaining(15.5), (30 - 15.5) / 30)
        hotp = AuthEntry.parse("otpauth://hotp/x?secret=JBSWY3DPEHPK3PXP&counter=1", "43")
        self.assertEqual(hotp.fraction_remaining(15), 1.0)


if __name__ == "__main__":
    unittest.main()

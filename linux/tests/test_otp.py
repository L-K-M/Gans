"""RFC 6238 (TOTP) and RFC 4226 (HOTP) published test vectors."""

import unittest

from gans import otp
from gans.otp import OTPAlgorithm

SHA1_SEED = b"12345678901234567890"
SHA256_SEED = b"12345678901234567890123456789012"
SHA512_SEED = b"1234567890123456789012345678901234567890123456789012345678901234"


def _totp(seed, time, algorithm):
    return otp.totp(seed, time, period=30, digits=8, algorithm=algorithm)


class TOTPGeneratorTests(unittest.TestCase):
    def test_rfc6238_sha1(self):
        self.assertEqual(_totp(SHA1_SEED, 59, OTPAlgorithm.SHA1), "94287082")
        self.assertEqual(_totp(SHA1_SEED, 1111111109, OTPAlgorithm.SHA1), "07081804")
        self.assertEqual(_totp(SHA1_SEED, 1111111111, OTPAlgorithm.SHA1), "14050471")
        self.assertEqual(_totp(SHA1_SEED, 1234567890, OTPAlgorithm.SHA1), "89005924")
        self.assertEqual(_totp(SHA1_SEED, 2000000000, OTPAlgorithm.SHA1), "69279037")
        self.assertEqual(_totp(SHA1_SEED, 20000000000, OTPAlgorithm.SHA1), "65353130")

    def test_digits_clamped_to_safe_range(self):
        huge = otp.code(SHA1_SEED, 1, 99, OTPAlgorithm.SHA1)
        self.assertEqual(len(huge), 9)
        self.assertEqual(huge, otp.code(SHA1_SEED, 1, 9, OTPAlgorithm.SHA1))
        self.assertEqual(len(otp.code(SHA1_SEED, 1, 0, OTPAlgorithm.SHA1)), 1)

    def test_rfc6238_sha256(self):
        self.assertEqual(_totp(SHA256_SEED, 59, OTPAlgorithm.SHA256), "46119246")
        self.assertEqual(_totp(SHA256_SEED, 1111111109, OTPAlgorithm.SHA256), "68084774")
        self.assertEqual(_totp(SHA256_SEED, 2000000000, OTPAlgorithm.SHA256), "90698825")

    def test_rfc6238_sha512(self):
        self.assertEqual(_totp(SHA512_SEED, 59, OTPAlgorithm.SHA512), "90693936")
        self.assertEqual(_totp(SHA512_SEED, 1111111111, OTPAlgorithm.SHA512), "99943326")
        self.assertEqual(_totp(SHA512_SEED, 20000000000, OTPAlgorithm.SHA512), "47863826")

    def test_rfc4226_hotp(self):
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        for counter, code in enumerate(expected):
            self.assertEqual(otp.code(SHA1_SEED, counter, 6, OTPAlgorithm.SHA1), code)

    def test_counter_is_clamped_into_uint64(self):
        # struct.pack(">Q") rejects negatives and >= 2**64; both are attacker-reachable.
        self.assertEqual(otp.code(SHA1_SEED, -1, 6, OTPAlgorithm.SHA1), otp.code(SHA1_SEED, 0, 6, OTPAlgorithm.SHA1))
        self.assertEqual(otp.code(SHA1_SEED, 2 ** 64 + 7, 6, OTPAlgorithm.SHA1),
                         otp.code(SHA1_SEED, 2 ** 64 - 1, 6, OTPAlgorithm.SHA1))
        self.assertEqual(len(otp.totp(SHA1_SEED, -1e9)), 6)  # pre-1970 clock

    def test_period_of_zero_or_less_does_not_divide_by_zero(self):
        self.assertEqual(otp.totp(SHA1_SEED, 59, period=0), otp.totp(SHA1_SEED, 59, period=1))
        self.assertEqual(otp.steam(SHA1_SEED, 59, period=-5), otp.steam(SHA1_SEED, 59, period=1))
        self.assertEqual(otp.seconds_remaining(59, period=0), 1)

    def test_seconds_remaining(self):
        self.assertEqual(otp.seconds_remaining(0, 30), 30)
        self.assertEqual(otp.seconds_remaining(10, 30), 20)
        self.assertEqual(otp.seconds_remaining(29, 30), 1)

    def test_steam_is_deterministic_and_well_formed(self):
        code = otp.steam(SHA1_SEED, 59)
        self.assertEqual(len(code), 5)
        alphabet = set("23456789BCDFGHJKMNPQRTVWXY")
        self.assertTrue(all(ch in alphabet for ch in code))
        # Stable within the same 30s window (t=30 and t=59 are both window 1).
        self.assertEqual(code, otp.steam(SHA1_SEED, 30))

    def test_lenient_algorithm(self):
        self.assertIs(OTPAlgorithm.lenient("sha256"), OTPAlgorithm.SHA256)
        self.assertIs(OTPAlgorithm.lenient("SHA512"), OTPAlgorithm.SHA512)
        self.assertIs(OTPAlgorithm.lenient("BOGUS"), OTPAlgorithm.SHA1)
        self.assertIs(OTPAlgorithm.lenient(None), OTPAlgorithm.SHA1)


if __name__ == "__main__":
    unittest.main()

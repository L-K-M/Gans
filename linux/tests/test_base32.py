"""RFC 4648 §10 base32 test vectors."""

import unittest

from gans import base32


def _decode(text):
    raw = base32.decode(text)
    return None if raw is None else raw.decode("utf-8")


class Base32Tests(unittest.TestCase):
    def test_rfc4648_vectors(self):
        self.assertEqual(_decode("MY======"), "f")
        self.assertEqual(_decode("MZXQ===="), "fo")
        self.assertEqual(_decode("MZXW6==="), "foo")
        self.assertEqual(_decode("MZXW6YQ="), "foob")
        self.assertEqual(_decode("MZXW6YTB"), "fooba")
        self.assertEqual(_decode("MZXW6YTBOI======"), "foobar")

    def test_tolerates_lowercase_spaces_and_missing_padding(self):
        self.assertEqual(_decode("mzxw6ytb"), "fooba")
        self.assertEqual(_decode("MZXW 6YTB"), "fooba")
        self.assertEqual(_decode("MZXW6YTBOI"), "foobar")  # no padding
        self.assertEqual(_decode("MZXW-6YTB"), "fooba")
        self.assertEqual(_decode("MZXW6YTB\n"), "fooba")     # pasted with a trailing newline
        self.assertEqual(_decode("\tMZXW 6YTB\r\n"), "fooba")
        self.assertEqual(_decode("MZXW6YTBOI======"), "foobar")

    def test_rejects_invalid_characters(self):
        self.assertIsNone(base32.decode("0189"))  # 0, 1, 8, 9 aren't in the alphabet
        self.assertIsNone(base32.decode(""))
        self.assertIsNone(base32.decode("===="))
        self.assertIsNone(base32.decode("=MZXW6YTB"))  # leading '=' is never valid padding

    def test_known_authenticator_seed(self):
        # The RFC 6238 SHA1 seed "12345678901234567890".
        self.assertEqual(_decode("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"), "12345678901234567890")


if __name__ == "__main__":
    unittest.main()

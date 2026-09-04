import unittest

from gans import b64


class Base64Tests(unittest.TestCase):
    def test_standard_round_trip(self):
        data = bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x00, 0x10])
        self.assertEqual(b64.decode_standard(b64.encode_standard(data)), data)

    def test_standard_tolerates_missing_padding(self):
        # "foob" -> "Zm9vYg==" ; without padding it should still decode.
        self.assertEqual(b64.decode_standard("Zm9vYg"), b"foob")
        self.assertEqual(b64.decode_standard("Zm9vYg=="), b"foob")

    def test_standard_rejects_garbage(self):
        self.assertIsNone(b64.decode_standard("not base64!"))
        self.assertIsNone(b64.decode_standard("Zm9v Yg=="))

    def test_url_safe_round_trip(self):
        data = bytes(range(41))
        encoded = b64.encode_url_safe(data)
        self.assertNotIn("+", encoded)
        self.assertNotIn("/", encoded)
        self.assertNotIn("=", encoded)
        self.assertEqual(b64.decode_url_safe(encoded), data)

    def test_url_safe_padded_keeps_padding_but_stays_url_safe(self):
        # "foob" -> standard "Zm9vYg==" ; padded url-safe must keep the "==".
        padded = b64.encode_url_safe_padded(b"foob")
        self.assertEqual(padded, "Zm9vYg==")
        self.assertNotIn("+", padded)
        self.assertNotIn("/", padded)
        # A pattern that yields '+'/'/' in standard base64 must use '-'/'_' instead.
        tricky = b64.encode_url_safe_padded(bytes([0xFB, 0xFF, 0xBF]))
        self.assertNotIn("+", tricky)
        self.assertNotIn("/", tricky)
        self.assertEqual(b64.decode_url_safe(tricky), bytes([0xFB, 0xFF, 0xBF]))

    def test_url_safe_decodes_standard_distinctly(self):
        data = bytes([0xFB, 0xFF, 0xBF])
        std = b64.encode_standard(data)
        self.assertTrue("+" in std or "/" in std)
        self.assertEqual(b64.decode_url_safe(b64.encode_url_safe(data)), data)


if __name__ == "__main__":
    unittest.main()

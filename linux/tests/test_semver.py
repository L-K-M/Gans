import unittest

from gans.semver import SemanticVersion as V


class SemanticVersionTests(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(V.parse("v1.2.3").components, [1, 2, 3])
        self.assertEqual(V.parse("1.4.0-beta.2").prerelease, "beta.2")
        self.assertIsNone(V.parse("not-a-version"))
        self.assertIsNone(V.parse(""))
        self.assertEqual(V.parse("1.2.3+build.7").components, [1, 2, 3])
        self.assertIsNone(V.parse("1.2.3+build.7").prerelease)

    def test_comparison_pads_components(self):
        self.assertEqual(V.parse("1.2"), V.parse("1.2.0"))
        self.assertTrue(V.parse("1.2.0") < V.parse("1.10.0"))
        self.assertTrue(V.parse("v0.9.0") < V.parse("0.10"))
        self.assertTrue(V.parse("1.5.0") > V.parse("1.4.9"))

    def test_prerelease_sorts_below_final(self):
        self.assertTrue(V.parse("1.2.0-beta") < V.parse("1.2.0"))
        self.assertFalse(V.parse("1.2.0") < V.parse("1.2.0-beta"))
        self.assertTrue(V.parse("1.2.0-alpha") < V.parse("1.2.0-beta"))

    def test_hash_agrees_with_eq(self):
        self.assertEqual(hash(V.parse("1.2")), hash(V.parse("1.2.0")))
        self.assertEqual(len({V.parse("1.2"), V.parse("1.2.0"), V.parse("v1.2.0.0")}), 1)
        self.assertNotEqual(hash(V.parse("1.2.0-beta")), hash(V.parse("1.2.0")))

    def test_non_decimal_digits_do_not_raise(self):
        self.assertIsNone(V.parse("v1.²"))
        self.assertEqual(V.parse("1.٣").components, [1, 3])  # Unicode decimals are what int() accepts

    def test_str(self):
        self.assertEqual(str(V.parse(" v1.2.3 ")), "v1.2.3")


if __name__ == "__main__":
    unittest.main()

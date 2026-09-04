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

    def test_str(self):
        self.assertEqual(str(V.parse(" v1.2.3 ")), "v1.2.3")


if __name__ == "__main__":
    unittest.main()

"""IssuerChip's pure helpers (headless): the FNV-1a-style hue fold matches the macOS app
byte for byte, and the initials follow the Swift rules."""

import unittest

from gans.ui.issuerchip import chip_gradient, hue_for, initials_for

#: Degrees (hash % 360) computed with the macOS app's constants. Note the offset basis
#: there is 1469598103934665603 — the standard FNV-1a basis minus its last digit — so
#: the published FNV vectors do *not* apply; chip colors must match across platforms,
#: so the port keeps the app's constant rather than "fixing" it.
REFERENCE_DEGREES = {
    "a": 6,
    "GitHub": 272,
    "Amazon Web Services": 103,
    "aws": 336,
    "Café": 190,
}


class HueTests(unittest.TestCase):
    def test_matches_the_macos_reference_values(self):
        for text, degrees in REFERENCE_DEGREES.items():
            self.assertAlmostEqual(hue_for(text), degrees / 360.0, places=12)

    def test_empty_name_is_hue_zero(self):
        self.assertEqual(hue_for(""), 0.0)
        self.assertNotEqual(hue_for("   "), 0.0)  # folding keeps spaces: not empty, so hashed

    def test_hashes_the_search_folded_name(self):
        self.assertEqual(hue_for("GitHub"), hue_for("github"))
        self.assertEqual(hue_for("GITHUB"), hue_for("github"))
        self.assertEqual(hue_for("Café"), hue_for("cafe"))
        self.assertNotEqual(hue_for("GitHub"), hue_for("GitLab"))

    def test_range(self):
        for name in ("GitHub", "Amazon Web Services", "aws", "Zulu", "x", "🦆"):
            hue = hue_for(name)
            self.assertGreaterEqual(hue, 0.0)
            self.assertLess(hue, 1.0)

    def test_hue_is_a_multiple_of_one_degree(self):
        self.assertAlmostEqual((hue_for("GitHub") * 360) % 1, 0.0, places=9)

    def test_gradient_darkens_towards_the_bottom(self):
        top, bottom = chip_gradient("GitHub")
        self.assertGreater(sum(top), sum(bottom))
        for component in top + bottom:
            self.assertGreaterEqual(component, 0.0)
            self.assertLessEqual(component, 1.0)


class InitialsTests(unittest.TestCase):
    def test_two_words_use_their_first_letters(self):
        self.assertEqual(initials_for("Amazon Web Services"), "AW")
        self.assertEqual(initials_for("proton mail"), "PM")

    def test_any_of_space_dash_underscore_dot_separates_words(self):
        self.assertEqual(initials_for("foo-bar"), "FB")
        self.assertEqual(initials_for("foo_bar"), "FB")
        self.assertEqual(initials_for("foo.bar"), "FB")
        self.assertEqual(initials_for("foo--bar"), "FB")  # empty parts are dropped

    def test_single_word_uses_its_first_two_characters(self):
        self.assertEqual(initials_for("GitHub"), "GI")
        self.assertEqual(initials_for("x"), "X")
        self.assertEqual(initials_for("ab"), "AB")

    def test_whitespace_and_empty_fall_back_to_a_bullet(self):
        self.assertEqual(initials_for(""), "•")
        self.assertEqual(initials_for("   "), "•")
        self.assertEqual(initials_for(" a"), "A")

    def test_non_letters_are_kept(self):
        self.assertEqual(initials_for("1Password"), "1P")
        self.assertEqual(initials_for("🦆"), "🦆")


if __name__ == "__main__":
    unittest.main()

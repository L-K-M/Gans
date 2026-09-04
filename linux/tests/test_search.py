import unittest

from gans import search
from gans.entry import AuthEntry, Kind
from gans.otp import OTPAlgorithm


def entry(issuer, account, **overrides):
    return AuthEntry(id=f"{issuer}-{account}", kind=Kind.totp(), issuer=issuer, account=account,
                     secret=bytes([1, 2, 3]), algorithm=OTPAlgorithm.SHA1, digits=6, period=30, **overrides)


ENTRIES = [
    entry("GitHub", "alice"),
    entry("GitLab", "bob"),
    entry("Google", "carol@github.io"),
    entry("Amazon", "dave"),
]


class SearchFilterTests(unittest.TestCase):
    def test_empty_query_returns_all_sorted_by_name(self):
        result = search.filter(ENTRIES, "  ")
        self.assertEqual([e.issuer for e in result], ["Amazon", "GitHub", "GitLab", "Google"])

    def test_prefix_matches_rank_above_substring(self):
        result = search.filter(ENTRIES, "git")
        self.assertEqual(sorted(e.issuer for e in result[:2]), ["GitHub", "GitLab"])
        self.assertEqual(result[-1].issuer, "Google")

    def test_case_and_diacritic_insensitive(self):
        self.assertEqual(len(search.filter([entry("Crédit", "x")], "credit")), 1)
        self.assertEqual(search.filter(ENTRIES, "AMAZON")[0].issuer, "Amazon")
        self.assertEqual(search.fold("Bäckerei"), "backerei")

    def test_account_match(self):
        self.assertEqual(search.filter(ENTRIES, "dave")[0].issuer, "Amazon")

    def test_no_match(self):
        self.assertEqual(search.filter(ENTRIES, "zzzzz"), [])

    def test_subsequence_fuzzy_match(self):
        self.assertEqual(search.filter(ENTRIES, "ghb")[0].issuer, "GitHub")

    def test_literal_match_outranks_fuzzy(self):
        self.assertEqual(search.filter(ENTRIES, "gitl")[0].issuer, "GitLab")

    def test_is_subsequence(self):
        self.assertTrue(search.is_subsequence("ghb", "github"))
        self.assertTrue(search.is_subsequence("", "anything"))
        self.assertFalse(search.is_subsequence("bhg", "github"))
        self.assertFalse(search.is_subsequence("xyz", "github"))

    def test_recently_used_floats_to_top_for_empty_query(self):
        result = search.filter(ENTRIES, "", ["Google-carol@github.io", "GitLab-bob"])
        self.assertEqual([e.issuer for e in result], ["Google", "GitLab", "Amazon", "GitHub"])

    def test_recency_breaks_ties_within_same_rank(self):
        self.assertEqual(search.filter(ENTRIES, "git", ["GitLab-bob"])[0].issuer, "GitLab")

    def test_multi_token_query_matches_across_fields_in_any_order(self):
        self.assertEqual([e.issuer for e in search.filter(ENTRIES, "github alice")], ["GitHub"])
        self.assertEqual([e.issuer for e in search.filter(ENTRIES, "alice github")], ["GitHub"])
        self.assertEqual(search.filter(ENTRIES, "github dave"), [])  # AND, not OR

    def test_multi_token_rank_uses_worst_token(self):
        self.assertEqual([e.issuer for e in search.filter(ENTRIES, "git bob")], ["GitLab"])

    def test_pinned_floats_to_top_for_empty_query_and_within_rank(self):
        mixed = [ENTRIES[0], ENTRIES[1], entry("Google", "carol@github.io", pinned=True), ENTRIES[3]]
        self.assertEqual(search.filter(mixed, "")[0].issuer, "Google")
        self.assertEqual(search.filter(mixed, "g", ["GitHub-alice"])[0].issuer, "Google")

    def test_tag_filter_with_hash_token(self):
        work = entry("GitHub", "alice", tags=("Work", "dev"))
        personal = entry("GitLab", "bob", tags=("personal",))
        both = [work, personal]
        self.assertEqual([e.issuer for e in search.filter(both, "#work")], ["GitHub"])
        self.assertEqual([e.issuer for e in search.filter(both, "#personal")], ["GitLab"])
        self.assertEqual([e.issuer for e in search.filter(both, "git #work")], ["GitHub"])
        self.assertEqual(search.filter(both, "#nope"), [])
        self.assertEqual(len(search.filter(both, "#")), 2)

    def test_next_index_clamps(self):
        self.assertEqual(search.next_index(3, 0, True), 1)
        self.assertEqual(search.next_index(3, 2, True), 2)
        self.assertEqual(search.next_index(3, 0, False), 0)
        self.assertEqual(search.next_index(3, None, True), 0)
        self.assertEqual(search.next_index(3, None, False), 2)
        self.assertIsNone(search.next_index(0, None, True))


if __name__ == "__main__":
    unittest.main()

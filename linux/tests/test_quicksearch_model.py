"""QuickSearchModel semantics (headless, no GTK): selection tracked by entry id, re-anchor
on query change, selection kept across a background refresh, clamped arrow navigation,
and synchronous observers."""

import unittest

from gans.entry import AuthEntry
from gans.ui.quicksearch_model import QuickSearchModel

SECRET = "JBSWY3DPEHPK3PXP"


def entry(entry_id, issuer, account="alice@example.com", pinned=False):
    display = "&codeDisplay=%7B%22pinned%22%3Atrue%7D" if pinned else ""
    parsed = AuthEntry.parse(f"otpauth://totp/{issuer}:{account}?secret={SECRET}&issuer={issuer}{display}", entry_id)
    assert parsed is not None
    return parsed


ENTRIES = [
    entry("aws", "Amazon Web Services", "root"),
    entry("gh", "GitHub"),
    entry("gg", "Google"),
    entry("gl", "GitLab"),
]


class QuickSearchModelTests(unittest.TestCase):
    def setUp(self):
        self.model = QuickSearchModel()
        self.changes = 0

        def changed():
            self.changes += 1
        self.model.on_change(changed)
        self.model.set_entries(ENTRIES)

    def ids(self):
        return [item.id for item in self.model.results]

    # MARK: Results and selection

    def test_empty_query_lists_everything_in_name_order_and_selects_the_top(self):
        self.assertEqual(self.ids(), ["aws", "gh", "gl", "gg"])
        self.assertEqual(self.model.selected_id, "aws")
        self.assertIs(self.model.selected_entry, ENTRIES[0])
        self.assertTrue(self.model.has_entries)

    def test_query_change_reanchors_to_the_top_result(self):
        self.model.selected_id = "gl"
        self.model.query = "gi"
        # Prefix hits first (GitHub, GitLab); Google trails as a fuzzy subsequence match.
        self.assertEqual(self.ids()[:2], ["gh", "gl"])
        self.assertEqual(self.model.selected_id, "gh")

    def test_query_with_no_matches_clears_the_selection(self):
        self.model.query = "zzz"
        self.assertEqual(self.model.results, [])
        self.assertIsNone(self.model.selected_id)
        self.assertIsNone(self.model.selected_entry)
        self.assertTrue(self.model.has_entries)  # the vault still has entries

    def test_refresh_keeps_the_selection_when_the_entry_survives(self):
        self.model.selected_id = "gg"
        refreshed = [entry("gg", "Google"), entry("gh", "GitHub"), entry("new", "Newcomer")]
        self.model.set_entries(refreshed)
        self.assertEqual(self.model.selected_id, "gg")
        self.assertEqual(self.model.selected_entry.issuer, "Google")

    def test_refresh_reanchors_when_the_selected_entry_is_gone(self):
        self.model.selected_id = "gg"
        self.model.set_entries([entry("gh", "GitHub"), entry("gl", "GitLab")])
        self.assertEqual(self.model.selected_id, "gh")

    def test_recent_ids_reorder_but_keep_the_selection(self):
        self.model.selected_id = "gl"
        self.model.recent_ids = ["gg", "gh"]
        self.assertEqual(self.ids(), ["gg", "gh", "aws", "gl"])
        self.assertEqual(self.model.selected_id, "gl")
        self.assertEqual(self.model.recent_ids, ["gg", "gh"])

    def test_pinned_entries_float_to_the_top(self):
        self.model.set_entries(ENTRIES + [entry("pin", "Zulu", pinned=True)])
        self.assertEqual(self.ids()[0], "pin")

    def test_reset_clears_the_query_and_reanchors(self):
        self.model.query = "goo"
        self.model.reset()
        self.assertEqual(self.model.query, "")
        self.assertEqual(self.ids(), ["aws", "gh", "gl", "gg"])
        self.assertEqual(self.model.selected_id, "aws")

    def test_no_entries_at_all(self):
        self.model.set_entries([])
        self.assertFalse(self.model.has_entries)
        self.assertEqual(self.model.results, [])
        self.assertIsNone(self.model.selected_id)

    # MARK: Arrow navigation

    def test_move_selection_clamps_at_both_ends(self):
        self.model.move_selection(down=False)
        self.assertEqual(self.model.selected_id, "aws")  # already at the top
        for expected in ("gh", "gl", "gg", "gg"):
            self.model.move_selection(down=True)
            self.assertEqual(self.model.selected_id, expected)
        self.model.move_selection(down=False)
        self.assertEqual(self.model.selected_id, "gl")

    def test_move_selection_anchors_when_nothing_valid_is_selected(self):
        self.model.selected_id = None
        self.model.move_selection(down=True)
        self.assertEqual(self.model.selected_id, "aws")
        self.model.selected_id = "not-a-result"
        self.model.move_selection(down=False)
        self.assertEqual(self.model.selected_id, "gg")  # up from nowhere → last

    def test_move_selection_with_no_results(self):
        self.model.query = "nothing here"
        self.model.move_selection(down=True)
        self.assertIsNone(self.model.selected_id)

    # MARK: Display flags

    def test_codes_visible_is_the_preference_or_a_live_peek(self):
        self.assertFalse(self.model.codes_visible)
        self.model.peek = True
        self.assertTrue(self.model.codes_visible)
        self.model.peek = False
        self.assertFalse(self.model.codes_visible)
        self.model.show_codes = True
        self.assertTrue(self.model.codes_visible)

    def test_target_app_name_defaults_to_none(self):
        self.assertIsNone(self.model.target_app_name)
        self.model.target_app_name = "Firefox"
        self.assertEqual(self.model.target_app_name, "Firefox")

    # MARK: Observers

    def test_observers_fire_synchronously_on_every_change(self):
        before = self.changes
        self.model.query = "g"
        self.assertEqual(self.changes, before + 1)
        self.model.move_selection(down=True)
        self.assertEqual(self.changes, before + 2)
        self.model.tick = 12345.0
        self.assertEqual(self.changes, before + 3)
        self.assertEqual(self.model.tick, 12345.0)
        self.model.show_indices = True
        self.model.show_indices = True  # unchanged flags don't re-render
        self.assertEqual(self.changes, before + 4)
        self.model.selected_id = self.model.selected_id
        self.assertEqual(self.changes, before + 4)

    def test_a_failing_observer_does_not_break_the_others(self):
        seen = []

        def broken():
            raise RuntimeError("boom")
        self.model.on_change(broken)
        self.model.on_change(lambda: seen.append(True))
        with self.assertLogs("gans.app", level="ERROR"):
            self.model.query = "git"
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()

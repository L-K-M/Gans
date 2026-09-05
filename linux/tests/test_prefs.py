import json
import os
import tempfile
import unittest
from pathlib import Path

from gans.hotkeyspec import HotkeySpec
from gans.prefs import DeliveryMode, Preferences


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "prefs.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_defaults(self):
        prefs = Preferences(self.path)
        self.assertEqual(prefs.hotkey, HotkeySpec.DEFAULT)
        self.assertIs(prefs.delivery_mode, DeliveryMode.TYPE)
        self.assertTrue(prefs.also_copy_when_typing)
        self.assertFalse(prefs.require_unlock)
        self.assertFalse(prefs.show_codes_in_quick_search)
        self.assertTrue(prefs.update_checks_enabled)

    def test_hotkey_persists_and_decodes(self):
        custom = HotkeySpec(key="F9", shift=True, super_=True)
        Preferences(self.path).hotkey = custom
        self.assertEqual(Preferences(self.path).hotkey, custom)

    def test_record_usage_deduplicates_orders_and_persists(self):
        prefs = Preferences(self.path)
        prefs.record_usage("a")
        prefs.record_usage("b")
        prefs.record_usage("a")  # re-using "a" moves it back to the front, no dupe
        self.assertEqual(prefs.recently_used_ids, ["a", "b"])
        self.assertEqual(Preferences(self.path).recently_used_ids, ["a", "b"])

    def test_recent_limit(self):
        prefs = Preferences(self.path)
        for index in range(Preferences.RECENT_LIMIT + 10):
            prefs.record_usage(str(index))
        self.assertEqual(len(prefs.recently_used_ids), Preferences.RECENT_LIMIT)
        self.assertEqual(prefs.recently_used_ids[0], str(Preferences.RECENT_LIMIT + 9))

    def test_clipboard_clear_defaults_and_delay(self):
        prefs = Preferences(self.path)
        self.assertTrue(prefs.clear_clipboard_enabled)
        self.assertEqual(prefs.clear_clipboard_seconds, 30)
        self.assertEqual(prefs.clipboard_clear_delay, 30)
        prefs.clear_clipboard_enabled = False
        self.assertIsNone(prefs.clipboard_clear_delay)

    def test_frecency_counts_persist_and_rank(self):
        prefs = Preferences(self.path)
        for entry_id in ["a", "c", "c", "a", "a", "b"]:
            prefs.record_usage(entry_id)
        self.assertEqual(prefs.usage_counts["a"], 3)
        self.assertEqual(prefs.usage_counts["c"], 2)
        self.assertEqual(prefs.usage_counts["b"], 1)
        self.assertEqual([item[0] for item in prefs.most_used(2)], ["a", "c"])
        self.assertEqual(prefs.frecency_ranked_ids, ["a", "c", "b"])
        self.assertEqual(Preferences(self.path).usage_counts["a"], 3)

    def test_honk_and_onboarding_default_off_and_persist(self):
        self.assertFalse(Preferences(self.path).honk_on_copy)
        self.assertFalse(Preferences(self.path).has_completed_onboarding)
        prefs = Preferences(self.path)
        prefs.honk_on_copy = True
        prefs.has_completed_onboarding = True
        reloaded = Preferences(self.path)
        self.assertTrue(reloaded.honk_on_copy)
        self.assertTrue(reloaded.has_completed_onboarding)

    def test_delivery_mode_persists(self):
        prefs = Preferences(self.path)
        prefs.delivery_mode = DeliveryMode.PASTE
        prefs.also_copy_when_typing = False
        reloaded = Preferences(self.path)
        self.assertIs(reloaded.delivery_mode, DeliveryMode.PASTE)
        self.assertFalse(reloaded.also_copy_when_typing)

    def test_corrupt_file_falls_back_to_defaults(self):
        self.path.write_text("{not json")
        self.assertIs(Preferences(self.path).delivery_mode, DeliveryMode.TYPE)
        self.path.write_text(json.dumps({"deliveryMode": "bogus", "quickSearchHotkey": 12}))
        prefs = Preferences(self.path)
        self.assertIs(prefs.delivery_mode, DeliveryMode.TYPE)
        self.assertEqual(prefs.hotkey, HotkeySpec.DEFAULT)

    def test_file_is_private(self):
        Preferences(self.path).honk_on_copy = True
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_observers_fire(self):
        prefs = Preferences(self.path)
        seen = []
        prefs.on_change(lambda: seen.append(1))
        prefs.honk_on_copy = True
        prefs.record_usage("x")
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()

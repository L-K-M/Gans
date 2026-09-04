import unittest

from gans.hotkeyspec import HotkeySpec


class HotkeySpecTests(unittest.TestCase):
    def test_default_is_control_alt_space(self):
        spec = HotkeySpec.DEFAULT
        self.assertEqual(spec.key, "space")
        self.assertEqual(spec.accelerator, "<Control><Alt>space")
        self.assertEqual(spec.display_string, "Ctrl+Alt+Space")
        self.assertEqual(spec.portal_trigger, "CTRL+ALT+space")

    def test_display_string_orders_modifiers(self):
        spec = HotkeySpec(key="e", control=True, alt=True, shift=True, super_=True)
        self.assertEqual(spec.display_string, "Ctrl+Alt+Shift+Super+E")
        self.assertEqual(spec.accelerator, "<Control><Alt><Shift><Super>e")

    def test_accelerator_round_trip(self):
        for text in ["<Control><Alt>space", "<Shift><Super>F9", "<Primary>a", "<Mod1><Mod4>Return"]:
            spec = HotkeySpec.from_accelerator(text)
            self.assertIsNotNone(spec, text)
            self.assertEqual(HotkeySpec.from_accelerator(spec.accelerator), spec)
        self.assertEqual(HotkeySpec.from_accelerator("<Primary>a"), HotkeySpec(key="a", control=True))
        self.assertEqual(HotkeySpec.from_accelerator("<Mod1><Mod4>Return"), HotkeySpec(key="Return", alt=True, super_=True))

    def test_rejects_non_gdk_notation(self):
        self.assertIsNone(HotkeySpec.from_accelerator("ctrl+alt+a"))
        self.assertIsNone(HotkeySpec.from_accelerator("Ctrl+Alt+Space"))
        self.assertIsNone(HotkeySpec.from_accelerator("<Control>a+b"))
        for text in ["<Control>a", "<Alt>space", "<Shift>F1", "<Super>minus", "<Control>KP_Enter", "<Alt>dead_acute"]:
            self.assertIsNotNone(HotkeySpec.from_accelerator(text), text)

    def test_rejects_malformed(self):
        self.assertIsNone(HotkeySpec.from_accelerator("<Control>"))
        self.assertIsNone(HotkeySpec.from_accelerator("<Bogus>a"))
        self.assertIsNone(HotkeySpec.from_accelerator(""))
        self.assertIsNone(HotkeySpec.from_accelerator("<Control>a b"))
        self.assertIsNone(HotkeySpec.from_json(42))

    def test_json_round_trip(self):
        spec = HotkeySpec(key="F1", control=True)
        self.assertEqual(HotkeySpec.from_json(spec.to_json()), spec)

    def test_key_display_names(self):
        self.assertEqual(HotkeySpec(key="Return", control=True).key_display_name, "Return")
        self.assertEqual(HotkeySpec(key="f5", control=True).key_display_name, "F5")
        self.assertEqual(HotkeySpec(key="KP_Enter", control=True).key_display_name, "Enter")
        self.assertEqual(HotkeySpec(key="KP_Add", control=True).key_display_name, "Keypad Add")
        self.assertEqual(HotkeySpec(key="grave", control=True).key_display_name, "`")
        self.assertEqual(HotkeySpec(key="Escape", control=True).display_string, "Ctrl+Esc")

    def test_has_modifier(self):
        self.assertFalse(HotkeySpec(key="a").has_modifier)
        self.assertTrue(HotkeySpec(key="a", shift=True).has_modifier)


if __name__ == "__main__":
    unittest.main()

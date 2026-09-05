"""GnomeKeybinding against the real media-keys schema, on GSettings' in-memory backend so
nothing ever touches a real dconf. The backend is chosen when the first Gio.Settings is
created, so the environment variable is set before anything imports Gio."""

import os

os.environ["GSETTINGS_BACKEND"] = "memory"

import unittest  # noqa: E402
from unittest.mock import patch  # noqa: E402

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio
except (ImportError, ValueError):   # pragma: no cover - depends on the box
    Gio = None

from gans.hotkeyspec import HotkeySpec  # noqa: E402

SPEC = HotkeySpec.DEFAULT
OTHER = HotkeySpec(key="F12", super_=True)
OTHER_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"


@unittest.skipUnless(Gio is not None, "PyGObject/Gio not available")
class GnomeKeybindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gans.platform.gnome import GnomeKeybinding
        cls.Keybinding = GnomeKeybinding
        if not GnomeKeybinding.available():
            raise unittest.SkipTest("media-keys schema not installed (apt install gnome-settings-daemon-common)")
        backend = type(Gio.Settings.new(GnomeKeybinding.SCHEMA).props.backend).__name__
        if "Memory" not in backend:
            raise unittest.SkipTest(f"refusing to run against a real settings backend ({backend})")

    def setUp(self):
        self.parent = Gio.Settings.new(self.Keybinding.SCHEMA)
        self.parent.reset(self.Keybinding.LIST_KEY)

    def tearDown(self):
        self.Keybinding.remove()

    def paths(self):
        return list(self.parent.get_strv(self.Keybinding.LIST_KEY))

    def child(self):
        return Gio.Settings.new_with_path(self.Keybinding.CHILD_SCHEMA, self.Keybinding.PATH)

    def test_install_current_remove(self):
        self.assertIsNone(self.Keybinding.current())
        self.assertTrue(self.Keybinding.install(SPEC))
        self.assertEqual(self.paths(), [self.Keybinding.PATH])
        child = self.child()
        self.assertEqual(child.get_string("name"), "Gans Quick Search")
        self.assertEqual(child.get_string("command"), "gans toggle")
        self.assertEqual(child.get_string("binding"), "<Control><Alt>space")
        self.assertEqual(self.Keybinding.current(), SPEC)

        self.Keybinding.remove()
        self.assertEqual(self.paths(), [])
        self.assertIsNone(self.Keybinding.current())
        self.assertEqual(self.child().get_string("binding"), "")
        self.Keybinding.remove()   # idempotent

    def test_reinstall_updates_in_place(self):
        self.Keybinding.install(SPEC)
        self.Keybinding.install(OTHER, command="/opt/gans/bin/gans toggle")
        self.assertEqual(self.paths(), [self.Keybinding.PATH])   # not appended twice
        self.assertEqual(self.Keybinding.current(), OTHER)
        self.assertEqual(self.child().get_string("command"), "/opt/gans/bin/gans toggle")

    def test_other_custom_shortcuts_are_preserved(self):
        self.parent.set_strv(self.Keybinding.LIST_KEY, [OTHER_PATH])
        self.Keybinding.install(SPEC)
        self.assertEqual(self.paths(), [OTHER_PATH, self.Keybinding.PATH])
        self.Keybinding.remove()
        self.assertEqual(self.paths(), [OTHER_PATH])

    def test_current_ignores_orphaned_child_keys(self):
        # A binding value without our path in the list isn't active as far as GNOME is concerned.
        self.child().set_string("binding", SPEC.accelerator)
        self.assertIsNone(self.Keybinding.current())

    def test_everything_is_a_noop_without_the_schema(self):
        with patch.object(self.Keybinding, "available", return_value=False):
            self.assertFalse(self.Keybinding.install(SPEC))
            self.assertIsNone(self.Keybinding.current())
            self.Keybinding.remove()
        self.assertEqual(self.paths(), [])

    def test_available_false_without_a_schema_source(self):
        with patch.object(Gio.SettingsSchemaSource, "get_default", return_value=None):
            self.assertFalse(self.Keybinding.available())
        self.assertTrue(self.Keybinding.available())


if __name__ == "__main__":
    unittest.main()

"""LaunchAtLogin with a temporary ``XDG_CONFIG_HOME``."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gans.platform.autostart import LaunchAtLogin

REQUIRED_LINES = (
    "[Desktop Entry]", "Type=Application", "Name=Gans", "Comment=Ente Auth codes, one keystroke away",
    "Exec=gans", "Icon=ch.lkmc.Gans", "Terminal=false", "Categories=Utility;",
    "X-GNOME-Autostart-enabled=true", "StartupNotify=false",
)


class LaunchAtLoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.root / "autostart" / "ch.lkmc.Gans.desktop"

    def test_path_follows_xdg_config_home(self):
        self.assertEqual(LaunchAtLogin.path(), self.path)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(LaunchAtLogin.path(), Path.home() / ".config" / "autostart" / "ch.lkmc.Gans.desktop")

    def test_disabled_until_set(self):
        self.assertFalse(LaunchAtLogin.is_enabled())
        LaunchAtLogin.set(False)   # nothing to remove: no error
        self.assertFalse(self.path.exists())

    def test_enable_writes_the_desktop_entry(self):
        LaunchAtLogin.set(True)
        self.assertTrue(LaunchAtLogin.is_enabled())
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "[Desktop Entry]")
        for line in REQUIRED_LINES:
            self.assertIn(line, lines)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        self.assertEqual([entry for entry in os.listdir(self.path.parent)], ["ch.lkmc.Gans.desktop"])  # no temp litter

    def test_disable_removes_the_file(self):
        LaunchAtLogin.set(True)
        LaunchAtLogin.set(False)
        self.assertFalse(self.path.exists())
        self.assertFalse(LaunchAtLogin.is_enabled())

    def test_entry_switched_off_in_place_counts_as_disabled(self):
        self.path.parent.mkdir(parents=True)
        for line in ("Hidden=true", "hidden = TRUE", "X-GNOME-Autostart-enabled=false"):
            self.path.write_text(f"[Desktop Entry]\nType=Application\nName=Gans\nExec=gans\n{line}\n")
            self.assertFalse(LaunchAtLogin.is_enabled(), line)
        self.path.write_text("[Desktop Entry]\nType=Application\nName=Gans\nExec=gans\nHidden=false\n")
        self.assertTrue(LaunchAtLogin.is_enabled())
        LaunchAtLogin.set(True)   # re-enabling rewrites the entry
        self.assertIn("X-GNOME-Autostart-enabled=true", self.path.read_text())
        self.assertTrue(LaunchAtLogin.is_enabled())

    def test_only_the_desktop_entry_group_counts(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("# comment\n[Desktop Entry]\nName=Gans\nName[de]=Gans\nExec=gans\n\n[Desktop Action x]\nHidden=true\n")
        self.assertTrue(LaunchAtLogin.is_enabled())

    def test_failure_is_logged_not_raised(self):
        blocker = self.root / "autostart"
        blocker.write_text("not a directory")
        with self.assertLogs("gans.app", level="ERROR"):
            LaunchAtLogin.set(True)
        self.assertFalse(LaunchAtLogin.is_enabled())


if __name__ == "__main__":
    unittest.main()

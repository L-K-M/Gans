import os
import unittest
from unittest import mock

from gans.platform import session


def environ(**values):
    return mock.patch.dict(os.environ, values, clear=True)


class SessionTypeTests(unittest.TestCase):
    def test_declared_session_type_wins(self):
        with environ(XDG_SESSION_TYPE="wayland", DISPLAY=":0"):
            self.assertEqual(session.session_type(), "wayland")
        with environ(XDG_SESSION_TYPE="X11", WAYLAND_DISPLAY="wayland-0"):
            self.assertEqual(session.session_type(), "x11")

    def test_tty_session_falls_back_to_display_heuristics(self):
        with environ(XDG_SESSION_TYPE="tty", DISPLAY="localhost:10.0"):
            self.assertEqual(session.session_type(), "x11")
        with environ(XDG_SESSION_TYPE="unspecified", WAYLAND_DISPLAY="wayland-0", DISPLAY=":0"):
            self.assertEqual(session.session_type(), "wayland")

    def test_no_declaration_uses_sockets(self):
        with environ(WAYLAND_DISPLAY="wayland-1"):
            self.assertEqual(session.session_type(), "wayland")
        with environ(DISPLAY=":1"):
            self.assertEqual(session.session_type(), "x11")

    def test_nothing_graphical(self):
        with environ():
            self.assertEqual(session.session_type(), "none")
        with environ(XDG_SESSION_TYPE="tty", DISPLAY=""):
            self.assertEqual(session.session_type(), "none")

    def test_has_x_display(self):
        with environ(DISPLAY=":0"):
            self.assertTrue(session.has_x_display())
        with environ(DISPLAY=""):
            self.assertFalse(session.has_x_display())
        with environ():
            self.assertFalse(session.has_x_display())


class DesktopTests(unittest.TestCase):
    def test_desktop_is_lowercased(self):
        with environ(XDG_CURRENT_DESKTOP="ubuntu:GNOME"):
            self.assertEqual(session.desktop(), "ubuntu:gnome")
        with environ():
            self.assertEqual(session.desktop(), "")

    def test_gnome_flavours(self):
        for value in ("GNOME", "ubuntu:GNOME", "pop:GNOME", "GNOME-Classic:GNOME", "zorin:GNOME", "GNOME-Flashback:GNOME"):
            with environ(XDG_CURRENT_DESKTOP=value):
                self.assertTrue(session.is_gnome(), value)

    def test_other_desktops(self):
        for value in ("KDE", "XFCE", "X-Cinnamon", "MATE", "Unity", "LXQt", "sway"):
            with environ(XDG_CURRENT_DESKTOP=value):
                self.assertFalse(session.is_gnome(), value)

    def test_legacy_fallbacks_when_current_desktop_is_unset(self):
        with environ(DESKTOP_SESSION="gnome"):
            self.assertTrue(session.is_gnome())
        with environ(DESKTOP_SESSION="ubuntu"):
            self.assertTrue(session.is_gnome())
        with environ(DESKTOP_SESSION="plasma"):
            self.assertFalse(session.is_gnome())
        with environ(GNOME_DESKTOP_SESSION_ID="this-is-deprecated"):
            self.assertTrue(session.is_gnome())
        with environ():
            self.assertFalse(session.is_gnome())

    def test_current_desktop_overrides_legacy_hints(self):
        with environ(XDG_CURRENT_DESKTOP="KDE", DESKTOP_SESSION="gnome"):
            self.assertFalse(session.is_gnome())


if __name__ == "__main__":
    unittest.main()

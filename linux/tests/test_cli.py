import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from gans import cli
from gans.version import app_version


class CLITests(unittest.TestCase):
    def test_version_and_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), f"gans {app_version()}")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("gans toggle", out.getvalue())

    def test_unknown_command_and_option(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cli.main(["frobnicate"]), 2)
            self.assertEqual(cli.main(["--bogus"]), 2)
        self.assertIn("unknown", err.getvalue())

    def test_gdk_backend_choice(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True):
            self.assertEqual(cli.choose_gdk_backend(), "x11")
            self.assertEqual(os.environ["GDK_BACKEND"], "x11")
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True):
            self.assertIsNone(cli.choose_gdk_backend())
            self.assertNotIn("GDK_BACKEND", os.environ)
        with mock.patch.dict(os.environ, {"DISPLAY": ":1", "GANS_GDK_BACKEND": "wayland"}, clear=True):
            self.assertEqual(cli.choose_gdk_backend(), "wayland")
            self.assertEqual(os.environ["GDK_BACKEND"], "wayland")
        with mock.patch.dict(os.environ, {"DISPLAY": ":1", "GDK_BACKEND": "broadway"}, clear=True):
            self.assertEqual(cli.choose_gdk_backend(), "broadway")


if __name__ == "__main__":
    unittest.main()

"""End-to-end smoke test: launches the real tray app as a subprocess on a private Xvfb
display + session bus, drives it through the CLI's D-Bus forwarding (``gans settings``,
``gans toggle``, ``gans quit``), and checks the windows appear and the process exits
cleanly without tracebacks."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.harness import DisplaySession, gtk_available

LINUX_DIR = Path(__file__).resolve().parent.parent


def _xdotool_windows(name: str) -> list:
    try:
        result = subprocess.run(["xdotool", "search", "--name", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line for line in result.stdout.split() if line.strip()]


def _wait_for(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


@unittest.skipUnless(gtk_available() and shutil.which("xdotool"), "needs GTK and xdotool")
class AppEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = DisplaySession.start()
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name)
        cls.env = dict(os.environ)
        cls.env.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_RUNTIME_DIR": str(home / "run"),
            "GANS_NO_UPDATE_CHECK": "1",
            "GANS_DEBUG": "1",
            "XDG_CURRENT_DESKTOP": "XFCE",   # no GNOME schemas here → X11 grab / none backend
        })
        (home / "run").mkdir(mode=0o700)
        # A log file rather than PIPEs: nobody drains them until the end, and a chatty
        # GANS_DEBUG run could fill the 64 KiB pipe buffer and stall the app.
        cls.log_path = home / "app.log"
        cls.log_file = open(cls.log_path, "w", encoding="utf-8")
        try:
            cls.app = subprocess.Popen([sys.executable, "-m", "gans"], cwd=str(LINUX_DIR), env=cls.env,
                                       stdout=cls.log_file, stderr=subprocess.STDOUT, text=True)
        except Exception:
            cls.log_file.close()
            cls.session.stop()
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.app.poll() is None:
            cls.app.terminate()
            try:
                cls.app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.app.kill()
                cls.app.wait()
        cls.log_file.close()
        cls.session.stop()
        cls.tmp.cleanup()

    def _cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "gans", *args], cwd=str(LINUX_DIR), env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_lifecycle(self):
        # Not signed in and no keyring: the login window opens on its own after restore.
        self.assertTrue(_wait_for(lambda: _xdotool_windows("Sign in to Ente")), "login window did not appear")
        self.assertIsNone(self.app.poll(), "app exited early")

        settings = self._cli("settings")
        self.assertEqual(settings.returncode, 0, settings.stderr)
        self.assertTrue(_wait_for(lambda: _xdotool_windows("Gans Settings")), "settings window did not appear")

        toggle = self._cli("toggle")
        self.assertEqual(toggle.returncode, 0, toggle.stderr)
        time.sleep(1)
        self.assertIsNone(self.app.poll(), "app crashed on toggle")

        version = self._cli("--version")
        self.assertEqual(version.returncode, 0)
        self.assertTrue(version.stdout.startswith("gans "))

        quit_result = self._cli("quit")
        self.assertEqual(quit_result.returncode, 0, quit_result.stderr)
        self.assertTrue(_wait_for(lambda: self.app.poll() is not None, timeout=15), "app did not quit")
        self.log_file.flush()
        log = self.log_path.read_text(encoding="utf-8")
        self.assertEqual(self.app.returncode, 0, log)
        self.assertNotIn("Traceback", log)
        self.assertNotIn("CRITICAL", log)


if __name__ == "__main__":
    unittest.main()

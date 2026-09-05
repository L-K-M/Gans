"""End-to-end smoke test: launches the real tray app as a subprocess on a private Xvfb
display + session bus, drives it through the CLI's D-Bus forwarding (``gans settings``,
``gans toggle``, ``gans quit``), and checks the windows appear and the process exits
cleanly without tracebacks.

``AppSessionStartupTests`` boots the same ``GansApplication`` in-process (against a fake
Ente API and a keyring whose opening is held on an Event, like a pending GNOME Keyring
prompt) to check the session wiring around that startup window."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.harness import DisplaySession, gtk_available, pump, wait_until

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


# MARK: In-process startup

def _menu_labels(menu) -> list:
    return [child.get_label() for child in menu.get_children() if hasattr(child, "get_label")]


def _menu_row(menu, label):
    for child in menu.get_children():
        if hasattr(child, "get_label") and child.get_label() == label:
            return child
    raise AssertionError(f"no menu row {label!r} in {_menu_labels(menu)}")


@unittest.skipUnless(gtk_available(), "needs GTK")
class AppSessionStartupTests(unittest.TestCase):
    """One booted app (a second ``Gio.Application`` can't register in the same process),
    driven through the phases of a launch in a single test."""

    @classmethod
    def setUpClass(cls):
        from tests.gtkbind import gtk_session
        gtk_session()
        from gans.store.keyring import MemoryKeyring
        from gans.ui import app as app_module
        from tests.test_vault import FakeAPI, _entity, _fixture

        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name)
        (home / "run").mkdir(mode=0o700)
        cls.env = mock.patch.dict(os.environ, {
            "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "XDG_RUNTIME_DIR": str(home / "run"),
            "GANS_NO_UPDATE_CHECK": "1", "XDG_CURRENT_DESKTOP": "XFCE", "GANS_PKCHECK": "/nonexistent",
        })
        cls.env.start()

        cls.fixture = _fixture()
        cls.api = FakeAPI(cls.fixture)
        cls.api.pages = [(0, [_entity(cls.fixture["auth_key"], "e1",
                                      "otpauth://totp/GitHub:alice?secret=JBSWY3DPEHPK3PXP&issuer=GitHub", 1000)])]

        class PersistentMemoryKeyring(MemoryKeyring):
            persistent = True   # tells the "real" keyring apart from the interim one

        cls.real_keyring = PersistentMemoryKeyring()
        cls.keyring_gate = threading.Event()
        cls.patches = [
            mock.patch.object(app_module, "open_keyring", lambda: (cls.keyring_gate.wait(30), cls.real_keyring)[1]),
            mock.patch.object(app_module, "EnteAPI", lambda *args, **kwargs: cls.api),
        ]
        for patcher in cls.patches:
            patcher.start()
        cls.app_module = app_module
        cls.app = app_module.GansApplication()
        cls.app.register(None)   # primary on the private bus: emits startup → _boot
        if not cls.app._booted:
            raise unittest.SkipTest("the application did not boot")

    @classmethod
    def tearDownClass(cls):
        app = cls.app
        cls.keyring_gate.set()
        app.login_window.close()
        app.quick_search.hide(restore_focus=False)
        app.hotkey.unregister()
        app.x11.close()
        app.release()
        for patcher in cls.patches:
            patcher.stop()
        cls.env.stop()
        cls.tmp.cleanup()

    def test_session_startup(self):
        from gi.repository import GLib
        from gans.ente.models import AuthorizationResponse
        from gans.ente.vault import VaultState
        from tests.test_vault import PASSWORD

        app = self.app
        app_module = self.app_module
        toasts = []
        original_show = app.toast.show
        app.toast.show = lambda message, **kwargs: (toasts.append(message), original_show(message, **kwargs))

        # 1. The keyring prompt is pending: every sign-in entry point says "still starting"
        #    instead of opening a login that would land in the interim memory keyring.
        self.assertFalse(app._session_started)
        self.assertIn("Sign in to Ente…", _menu_labels(app.tray.menu))
        _menu_row(app.tray.menu, "Sign in to Ente…").activate()
        app.settings_window.on_sign_in()
        app._handle_command("ente-cli://passkey", True)
        app.quick_search.show()
        pump(100)
        self.assertEqual(toasts.count(app_module.STILL_STARTING), 4)
        self.assertIsNone(app.login_window.window)
        self.assertFalse(app.quick_search.is_visible)

        # 2. Belt and braces: a login that does complete in that window (the vault is the
        #    contract, not the UI) must not be discarded when the real keyring arrives.
        worker = threading.Thread(target=lambda: app.vault.complete_login(
            AuthorizationResponse.from_json(self.fixture["authorization"]), PASSWORD, "alice@example.com"))
        worker.start()
        worker.join(30)
        pump(100)
        self.assertTrue(app.vault.is_signed_in)
        self.assertFalse(app.vault.keyring_persistent)
        self.assertIsNone(self.real_keyring.get("ente.token"))

        last_sync = app.vault.last_sync
        self.keyring_gate.set()
        self.assertTrue(wait_until(lambda: app._session_started, 10))
        self.assertTrue(wait_until(lambda: app.vault.last_sync != last_sync and app.vault.state is VaultState.READY, 10))
        pump(300)   # _after_restore
        self.assertTrue(app.vault.is_signed_in)
        self.assertTrue(app.vault.keyring_persistent)
        self.assertEqual(self.real_keyring.get("ente.token"), self.fixture["token"].encode())
        self.assertEqual(self.real_keyring.get("ente.authKey"), self.fixture["auth_key"])
        self.assertEqual(self.real_keyring.get("ente.email"), b"alice@example.com")
        self.assertEqual([entry.issuer for entry in app.vault.entries], ["GitHub"])
        self.assertIsNone(app.login_window.window)   # never opened: the session stands
        labels = _menu_labels(app.tray.menu)
        self.assertIn("alice@example.com", labels)
        self.assertNotIn("Sign in to Ente…", labels)
        self.assertNotIn("Session not saved (no keyring)", labels)

        # 3. The auto-refresh throttle must span suspend: a refresh just before sleeping
        #    can't swallow the wake-up sync hours later.
        if hasattr(time, "CLOCK_BOOTTIME"):
            self.assertAlmostEqual(app_module._uptime(), time.clock_gettime(time.CLOCK_BOOTTIME), delta=1.0)
        refreshes = []
        app.vault.refresh = lambda: refreshes.append(threading.current_thread().name)
        try:
            base = app_module._uptime()
            app._last_auto_refresh = base
            app._refresh_if_stale()
            app._on_prepare_for_sleep(None, None, None, None, None, GLib.Variant("(b)", (True,)))
            pump(100)
            self.assertEqual(refreshes, [])   # throttled: refreshed a moment ago
            with mock.patch.object(app_module, "_uptime", lambda: base + 3600):   # slept an hour
                app._on_prepare_for_sleep(None, None, None, None, None, GLib.Variant("(b)", (False,)))
            self.assertTrue(wait_until(lambda: len(refreshes) == 1, 5))
            self.assertNotEqual(refreshes[0], threading.main_thread().name)
            self.assertGreaterEqual(app._last_auto_refresh, base + 3600)
        finally:
            del app.vault.refresh

        # 4. With the keyring open the same entry points do open the login window.
        app.vault.sign_out()
        pump(100)
        _menu_row(app.tray.menu, "Sign in to Ente…").activate()
        self.assertTrue(wait_until(lambda: app.login_window.window is not None and app.login_window.window.get_visible(), 5))
        app.login_window.close()
        self.assertEqual(toasts.count(app_module.STILL_STARTING), 4)


if __name__ == "__main__":
    unittest.main()

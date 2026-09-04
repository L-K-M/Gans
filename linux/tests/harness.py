"""Shared helpers for the GUI/X11 tests: a private Xvfb display + session bus, and a
GTK event pump. Import this **before** any ``gi`` import in a test module.

    from tests.harness import DisplaySession, pump, gtk_available

    class MyTests(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.session = DisplaySession.start()   # skips the test class if Xvfb is missing
        @classmethod
        def tearDownClass(cls):
            cls.session.stop()

Each session picks a free display number, so test modules can run concurrently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import unittest
from typing import Optional


def gtk_available() -> bool:
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def _free_display(start: int = 110) -> int:
    number = start + (os.getpid() % 500)
    while os.path.exists(f"/tmp/.X{number}-lock") or os.path.exists(f"/tmp/.X11-unix/X{number}"):
        number += 1
    return number


class DisplaySession:
    """An Xvfb server plus a private D-Bus session bus, exported through the environment
    (``DISPLAY``, ``DBUS_SESSION_BUS_ADDRESS``, ``GDK_BACKEND=x11``)."""

    def __init__(self, display: int, xvfb: subprocess.Popen, dbus: Optional[subprocess.Popen],
                 previous_env: dict):
        self.display = display
        self.display_name = f":{display}"
        self._xvfb = xvfb
        self._dbus = dbus
        self._previous_env = previous_env

    @classmethod
    def start(cls, width: int = 1280, height: int = 800) -> "DisplaySession":
        if shutil.which("Xvfb") is None:
            raise unittest.SkipTest("Xvfb is not installed")
        display = _free_display()
        xvfb = subprocess.Popen(["Xvfb", f":{display}", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not os.path.exists(f"/tmp/.X11-unix/X{display}"):
            if xvfb.poll() is not None or time.monotonic() > deadline:
                xvfb.kill()
                raise unittest.SkipTest("Xvfb failed to start")
            time.sleep(0.05)

        keys = ("DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "GDK_BACKEND", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
                "NO_AT_BRIDGE", "GTK_A11Y")
        previous = {key: os.environ.get(key) for key in keys}
        os.environ["DISPLAY"] = f":{display}"
        os.environ["GDK_BACKEND"] = "x11"
        os.environ["XDG_SESSION_TYPE"] = "x11"
        os.environ.pop("WAYLAND_DISPLAY", None)
        os.environ["NO_AT_BRIDGE"] = "1"   # no accessibility bus in the sandbox
        os.environ["GTK_A11Y"] = "none"

        dbus = None
        if shutil.which("dbus-daemon"):
            dbus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address", "--nopidfile"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            address = dbus.stdout.readline().strip()
            if address:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
        return cls(display, xvfb, dbus, previous)

    def stop(self) -> None:
        for process in (self._dbus, self._xvfb):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def pump(milliseconds: int = 100) -> None:
    """Runs the GTK main loop for roughly ``milliseconds`` so events and idles settle."""
    from gi.repository import GLib, Gtk
    deadline = time.monotonic() + milliseconds / 1000.0
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if time.monotonic() >= deadline:
            break
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)


def wait_until(predicate, timeout: float = 5.0, step_ms: int = 50) -> bool:
    """Pumps GTK until ``predicate()`` is true or ``timeout`` seconds elapse."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(step_ms)
    return bool(predicate())

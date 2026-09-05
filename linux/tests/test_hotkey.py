"""HotkeyManager backend selection. The collaborators (session, gnome, portal, x11) are
faked through ``sys.modules`` so these tests need neither a display nor the real
backends, and so a press can be counted to prove exactly one backend is live."""

import os
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from gans.hotkeyspec import HotkeySpec
from gans.platform.hotkey import HotkeyManager, HotkeyStatus, manual_instructions, toggle_command

SPEC = HotkeySpec.DEFAULT
OTHER = HotkeySpec(key="F12", super_=True)
#: What ``toggle_command`` resolves to for the packaged launcher; pinned in the manager tests.
COMMAND = "/usr/bin/gans toggle"


# MARK: Fakes

def make_gnome(events, available=True, install_ok=True, install_raises=False):
    class GnomeKeybinding:
        NAME = "Gans Quick Search"

        @classmethod
        def available(cls):
            return available

        @classmethod
        def install(cls, spec, command="gans toggle"):
            events.append(("gnome.install", spec, command))
            if install_raises:
                raise RuntimeError("dconf exploded")
            return install_ok

        @classmethod
        def remove(cls):
            events.append(("gnome.remove",))

    return GnomeKeybinding


def make_portal(events, bind_ok=False, bind_raises=False, trigger="Ctrl+Alt+Space", during_bind=None,
                close_aborts_bind=True):
    """``during_bind`` runs inside the first ``bind`` — standing in for whatever the nested
    main loop dispatches while the desktop's consent dialog is up. Like the real portal, a
    ``close`` from there makes that ``bind`` return False (unless ``close_aborts_bind`` is
    off, which models a backend that still reports success afterwards)."""
    class GlobalShortcutsPortal:
        instances = []
        pending_hook = [during_bind] if during_bind is not None else []

        def __init__(self, on_pressed, dispatch):
            self.on_pressed = on_pressed
            self.dispatch = dispatch
            self.trigger_description = None
            self.closed = False
            GlobalShortcutsPortal.instances.append(self)
            events.append(("portal.new",))

        def bind(self, spec):
            events.append(("portal.bind", spec))
            if bind_raises:
                raise RuntimeError("bus exploded")
            if GlobalShortcutsPortal.pending_hook:
                GlobalShortcutsPortal.pending_hook.pop()()
                if self.closed and close_aborts_bind:
                    return False
            if bind_ok:
                self.trigger_description = trigger
            return bind_ok

        def close(self):
            events.append(("portal.close",))
            self.closed = True
            self.on_pressed = None

        def press(self):
            if self.on_pressed is not None:
                self.dispatch(self.on_pressed)

    return GlobalShortcutsPortal


def make_grabber(events, register_ok=True):
    class X11HotkeyGrabber:
        instances = []

        def __init__(self, x11, dispatch):
            self.x11 = x11
            self.dispatch = dispatch
            self.on_pressed = None
            X11HotkeyGrabber.instances.append(self)

        def register(self, spec, on_pressed):
            events.append(("x11.register", spec))
            if register_ok:
                self.on_pressed = on_pressed
            return register_ok

        def unregister(self):
            events.append(("x11.unregister",))
            self.on_pressed = None

        def press(self):
            if self.on_pressed is not None:
                self.dispatch(self.on_pressed)

    return X11HotkeyGrabber


class FakeX11:
    def __init__(self, available=True):
        self.available = available
        self.has_xtest = available


def fake_modules(events, *, is_gnome=False, session_type="x11", gnome=None, portal=None, grabber=None):
    session = types.ModuleType("gans.platform.session")
    session.is_gnome = lambda: is_gnome
    session.session_type = lambda: session_type
    session.desktop = lambda: "GNOME" if is_gnome else "KDE"
    session.has_x_display = lambda: True
    gnome_module = types.ModuleType("gans.platform.gnome")
    gnome_module.GnomeKeybinding = gnome or make_gnome(events, available=False)
    portal_module = types.ModuleType("gans.platform.portal")
    portal_module.GlobalShortcutsPortal = portal or make_portal(events)
    x11_module = types.ModuleType("gans.platform.x11")
    x11_module.X11HotkeyGrabber = grabber or make_grabber(events)
    return {"gans.platform.session": session, "gans.platform.gnome": gnome_module,
            "gans.platform.portal": portal_module, "gans.platform.x11": x11_module}


# MARK: Tests

class HotkeyManagerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.presses = 0
        # The command the GNOME shortcut (and the instructions) name; pinned so the box's
        # PATH and argv don't leak in. ToggleCommandTests covers the resolution itself.
        patcher = patch("gans.platform.hotkey.toggle_command", return_value=COMMAND)
        patcher.start()
        self.addCleanup(patcher.stop)

    def on_pressed(self):
        self.presses += 1

    def manager(self, x11=None):
        return HotkeyManager(self.on_pressed, lambda fn: fn(), x11=x11)

    def names(self):
        return [event[0] for event in self.events]

    def test_gnome_backend_wins_and_is_updated_and_removed(self):
        gnome = make_gnome(self.events)
        portal = make_portal(self.events, bind_ok=True)
        modules = fake_modules(self.events, is_gnome=True, gnome=gnome, portal=portal)
        with patch.dict(sys.modules, modules):
            manager = self.manager(x11=FakeX11())
            status = manager.register(SPEC)
            self.assertEqual(status.backend, "gnome")
            self.assertTrue(status.ok)
            self.assertIn(f'"{COMMAND}"', status.detail)
            self.assertIn("Custom Shortcuts", status.detail)
            self.assertIs(manager.status, status)
            self.assertEqual(self.events, [("gnome.install", SPEC, COMMAND)])
            self.assertEqual(portal.instances, [])   # exactly one backend: no portal, no grab

            manager.register(OTHER)
            self.assertEqual(self.names(), ["gnome.install", "gnome.remove", "gnome.install"])
            self.assertEqual(self.events[-1], ("gnome.install", OTHER, COMMAND))
            self.assertEqual(manager.spec, OTHER)

            manager.unregister()
            self.assertEqual(self.names()[-1], "gnome.remove")
            self.assertEqual(manager.status.backend, "none")
            manager.unregister()   # idempotent
            self.assertEqual(self.names().count("gnome.remove"), 2)
        self.assertEqual(self.presses, 0)

    def test_gnome_without_schema_falls_through_to_portal(self):
        gnome = make_gnome(self.events, available=False)
        portal = make_portal(self.events, bind_ok=True, trigger="Meta+Space")
        with patch.dict(sys.modules, fake_modules(self.events, is_gnome=True, gnome=gnome, portal=portal)):
            status = self.manager(x11=FakeX11()).register(SPEC)
        self.assertEqual(status.backend, "portal")
        self.assertTrue(status.ok)
        self.assertIn("Meta+Space", status.detail)
        self.assertNotIn("gnome.install", self.names())

    def test_gnome_backend_names_the_launcher_it_was_started_from(self):
        # gnome-settings-daemon spawns the command through its own PATH, so a source-tree
        # run must write the launcher's absolute path, not a bare "gans".
        command = "'/home/me/Gans source/linux/bin/gans' toggle"
        gnome = make_gnome(self.events)
        with patch.dict(sys.modules, fake_modules(self.events, is_gnome=True, gnome=gnome)), \
                patch("gans.platform.hotkey.toggle_command", return_value=command):
            status = self.manager(x11=FakeX11()).register(SPEC)
        self.assertEqual(status.backend, "gnome")
        self.assertEqual(self.events, [("gnome.install", SPEC, command)])
        self.assertIn(f'"{command}"', status.detail)

    def test_gnome_without_a_nameable_launcher_falls_through(self):
        # `python -m gans` with no `gans` on PATH: a shortcut running "gans toggle" would do
        # nothing, so the backend must not claim success.
        gnome = make_gnome(self.events)
        with patch.dict(sys.modules, fake_modules(self.events, is_gnome=True, gnome=gnome)), \
                patch("gans.platform.hotkey.toggle_command", return_value=None):
            with self.assertLogs("gans.hotkey", level="INFO") as logs:
                status = self.manager(x11=FakeX11()).register(SPEC)
        self.assertEqual(status.backend, "x11")
        self.assertNotIn("gnome.install", self.names())
        self.assertTrue(any("launcher" in line for line in logs.output))

    def test_gnome_install_failure_falls_through(self):
        gnome = make_gnome(self.events, install_raises=True)
        with patch.dict(sys.modules, fake_modules(self.events, is_gnome=True, gnome=gnome)):
            manager = self.manager(x11=FakeX11())
            with self.assertLogs("gans.hotkey", level="ERROR"):
                status = manager.register(SPEC)
            self.assertEqual(status.backend, "x11")
            manager.unregister()
        self.assertNotIn("gnome.remove", self.names())   # never installed, so nothing to remove

    def test_portal_backend_presses_arrive_once_and_stop_after_unregister(self):
        portal = make_portal(self.events, bind_ok=True)
        with patch.dict(sys.modules, fake_modules(self.events, portal=portal)):
            manager = self.manager(x11=FakeX11())
            status = manager.register(SPEC)
            self.assertEqual(status.backend, "portal")
            self.assertEqual(self.events[-1], ("portal.bind", SPEC))
            self.assertNotIn("x11.register", self.names())
            portal.instances[-1].press()
            self.assertEqual(self.presses, 1)

            manager.unregister()
            self.assertEqual(self.names()[-1], "portal.close")
            portal.instances[-1].press()
            self.assertEqual(self.presses, 1)

    def test_x11_backend_on_x11_session(self):
        grabber = make_grabber(self.events)
        with patch.dict(sys.modules, fake_modules(self.events, grabber=grabber)):
            manager = self.manager(x11=FakeX11())
            status = manager.register(SPEC)
            self.assertEqual(status.backend, "x11")
            self.assertTrue(status.ok)
            self.assertNotIn("Wayland", status.detail)
            self.assertIn("Ctrl+Alt+Space", status.detail)
            grabber.instances[-1].press()
            self.assertEqual(self.presses, 1)
            manager.unregister()
            self.assertEqual(self.names(), ["portal.new", "portal.bind", "x11.register", "x11.unregister"])

    def test_x11_backend_on_wayland_warns(self):
        with patch.dict(sys.modules, fake_modules(self.events, session_type="wayland")):
            status = self.manager(x11=FakeX11()).register(SPEC)
        self.assertEqual(status.backend, "x11")
        self.assertTrue(status.ok)
        self.assertIn("Wayland", status.detail)
        self.assertIn("gans toggle", status.detail)

    def test_x11_grab_failure_reports_none_with_instructions(self):
        grabber = make_grabber(self.events, register_ok=False)
        with patch.dict(sys.modules, fake_modules(self.events, grabber=grabber)):
            manager = self.manager(x11=FakeX11())
            status = manager.register(SPEC)
            self.assertEqual(status.backend, "none")
            self.assertFalse(status.ok)
            self.assertIn("gans toggle", status.detail)
            manager.unregister()
        self.assertNotIn("x11.unregister", self.names())   # a failed grab isn't retained

    def test_reregister_leaves_exactly_one_live_grab(self):
        grabber = make_grabber(self.events)
        with patch.dict(sys.modules, fake_modules(self.events, grabber=grabber)):
            manager = self.manager(x11=FakeX11())
            manager.register(SPEC)
            manager.register(OTHER)
            self.assertEqual(self.names(), ["portal.new", "portal.bind", "x11.register",
                                            "x11.unregister", "portal.new", "portal.bind", "x11.register"])
            for instance in grabber.instances:
                instance.press()
        self.assertEqual(self.presses, 1)

    def test_reentrant_register_during_portal_consent_keeps_one_backend(self):
        # bind() runs a nested main loop while the desktop shows its consent dialog, and the
        # Settings recorder can re-register from there. Whether or not the abandoned bind()
        # still reports success afterwards, exactly one session may stay live.
        for close_aborts_bind in (True, False):
            with self.subTest(close_aborts_bind=close_aborts_bind):
                self.events.clear()
                self.presses = 0
                manager = None
                inner = []
                portal = make_portal(self.events, bind_ok=True, close_aborts_bind=close_aborts_bind,
                                     during_bind=lambda: inner.append(manager.register(OTHER)))
                with patch.dict(sys.modules, fake_modules(self.events, portal=portal)):
                    manager = self.manager(x11=FakeX11())
                    status = manager.register(SPEC)
                    self.assertEqual([s.backend for s in inner], ["portal"])
                    self.assertIs(status, inner[0])   # the superseded call reports what replaced it
                    self.assertIs(manager.status, status)
                    self.assertIn(OTHER.display_string, status.detail)
                    self.assertEqual(manager.spec, OTHER)
                    self.assertEqual(self.events[1], ("portal.bind", SPEC))
                    self.assertEqual(self.events[4], ("portal.bind", OTHER))   # after the re-entrant unregister
                    self.assertNotIn("x11.register", self.names())
                    first, second = portal.instances
                    self.assertTrue(first.closed)
                    self.assertFalse(second.closed)
                    for instance in portal.instances:
                        instance.press()
                    self.assertEqual(self.presses, 1)

                    manager.unregister()
                    self.assertTrue(second.closed)
                    for instance in portal.instances:
                        instance.press()
                    self.assertEqual(self.presses, 1)

    def test_unregister_during_portal_consent_installs_nothing(self):
        # Quitting while the consent dialog is up: the pending bind is abandoned and the
        # register() that waited on it must not install anything afterwards.
        manager = None
        portal = make_portal(self.events, bind_ok=True, during_bind=lambda: manager.unregister())
        with patch.dict(sys.modules, fake_modules(self.events, portal=portal)):
            manager = self.manager(x11=FakeX11())
            status = manager.register(SPEC)
            self.assertEqual(status.backend, "none")
            self.assertEqual(status.detail, "No hotkey registered.")
            self.assertIs(manager.status, status)
            self.assertEqual(self.names(), ["portal.new", "portal.bind", "portal.close"])
            self.assertTrue(portal.instances[0].closed)
            portal.instances[0].press()
        self.assertEqual(self.presses, 0)

    def test_no_backend_available(self):
        with patch.dict(sys.modules, fake_modules(self.events)):
            for x11 in (None, FakeX11(available=False)):
                status = self.manager(x11=x11).register(SPEC)
                self.assertEqual(status, HotkeyStatus("none", False, manual_instructions(SPEC)))
        self.assertNotIn("x11.register", self.names())

    def test_portal_exception_is_contained(self):
        portal = make_portal(self.events, bind_raises=True)
        with patch.dict(sys.modules, fake_modules(self.events, portal=portal)):
            with self.assertLogs("gans.hotkey", level="ERROR"):
                status = self.manager(x11=FakeX11()).register(SPEC)
        self.assertEqual(status.backend, "x11")

    def test_unimportable_backends_fall_through(self):
        # A missing typelib makes the module import fail; ``None`` in sys.modules simulates that.
        modules = fake_modules(self.events, is_gnome=True)
        modules["gans.platform.gnome"] = None
        modules["gans.platform.portal"] = None
        with patch.dict(sys.modules, modules):
            status = self.manager(x11=FakeX11()).register(SPEC)
            self.assertEqual(status.backend, "x11")
            status = self.manager(x11=None).register(SPEC)
            self.assertEqual(status.backend, "none")

    def test_manual_instructions(self):
        text = manual_instructions(SPEC)
        self.assertIn(f'"{COMMAND}"', text)
        self.assertIn("Ctrl+Alt+Space", text)
        self.assertIn(COMMAND, manual_instructions())
        manager = self.manager()
        self.assertEqual(manager.manual_instructions(), manual_instructions(None))
        with patch("gans.platform.hotkey.toggle_command", return_value=None):
            self.assertIn('"gans toggle"', manual_instructions(SPEC))   # the documented fallback


class ToggleCommandTests(unittest.TestCase):
    """How the shortcut command is resolved: the launcher this process started from, else
    the `gans` on PATH, else nothing (and always an absolute, shell-quoted path)."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "Gans source"   # a space: the path must be quoted
        (self.root / "bin").mkdir(parents=True)
        self.launcher = self.root / "bin" / "gans"
        self.launcher.write_text("#!/bin/sh\n")
        self.launcher.chmod(0o755)
        self.expected = f"{shlex.quote(os.path.realpath(self.launcher))} toggle"

    def which(self, table):
        return patch("gans.platform.hotkey.shutil.which", side_effect=lambda name: table.get(name))

    def test_started_from_the_launcher(self):
        with patch.object(sys, "argv", [str(self.launcher)]), self.which({}):
            self.assertEqual(toggle_command(), self.expected)

    def test_relative_launcher_path_is_made_absolute(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with patch.object(sys, "argv", ["./bin/gans"]), self.which({}):
            self.assertEqual(toggle_command(), self.expected)

    def test_bare_name_is_resolved_like_the_shell_did(self):
        # A shell leaves argv[0] as typed; a file called "gans" in the cwd must not be mistaken for it.
        with patch.object(sys, "argv", ["gans"]), self.which({"gans": str(self.launcher)}):
            self.assertEqual(toggle_command(), self.expected)

    def test_falls_back_to_the_gans_on_path(self):
        with patch.object(sys, "argv", ["/usr/lib/python3/unittest/__main__.py"]), \
                self.which({"gans": "/usr/bin/gans"}):
            self.assertEqual(toggle_command(), "/usr/bin/gans toggle")

    def test_none_when_nothing_is_invocable(self):
        with patch.object(sys, "argv", ["/usr/lib/python3/unittest/__main__.py"]), self.which({}):
            self.assertIsNone(toggle_command())
        self.launcher.chmod(0o644)   # not executable: the desktop couldn't spawn it either
        with patch.object(sys, "argv", [str(self.launcher)]), self.which({}):
            self.assertIsNone(toggle_command())
        with patch.object(sys, "argv", []), self.which({}):
            self.assertIsNone(toggle_command())


if __name__ == "__main__":
    unittest.main()

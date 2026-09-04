"""HotkeyManager backend selection. The collaborators (session, gnome, portal, x11) are
faked through ``sys.modules`` so these tests need neither a display nor the real
backends, and so a press can be counted to prove exactly one backend is live."""

import sys
import types
import unittest
from unittest.mock import patch

from gans.hotkeyspec import HotkeySpec
from gans.platform.hotkey import HotkeyManager, HotkeyStatus, manual_instructions

SPEC = HotkeySpec.DEFAULT
OTHER = HotkeySpec(key="F12", super_=True)


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


def make_portal(events, bind_ok=False, bind_raises=False, trigger="Ctrl+Alt+Space"):
    class GlobalShortcutsPortal:
        instances = []

        def __init__(self, on_pressed, dispatch):
            self.on_pressed = on_pressed
            self.dispatch = dispatch
            self.trigger_description = None
            GlobalShortcutsPortal.instances.append(self)
            events.append(("portal.new",))

        def bind(self, spec):
            events.append(("portal.bind", spec))
            if bind_raises:
                raise RuntimeError("bus exploded")
            if bind_ok:
                self.trigger_description = trigger
            return bind_ok

        def close(self):
            events.append(("portal.close",))
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
            self.assertIn("gans toggle", status.detail)
            self.assertIn("Custom Shortcuts", status.detail)
            self.assertIs(manager.status, status)
            self.assertEqual(self.events, [("gnome.install", SPEC, "gans toggle")])
            self.assertEqual(portal.instances, [])   # exactly one backend: no portal, no grab

            manager.register(OTHER)
            self.assertEqual(self.names(), ["gnome.install", "gnome.remove", "gnome.install"])
            self.assertEqual(self.events[-1], ("gnome.install", OTHER, "gans toggle"))
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
        self.assertIn('"gans toggle"', text)
        self.assertIn("Ctrl+Alt+Space", text)
        self.assertIn("gans toggle", manual_instructions())
        manager = self.manager()
        self.assertEqual(manager.manual_instructions(), manual_instructions(None))


if __name__ == "__main__":
    unittest.main()

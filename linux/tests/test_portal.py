"""GlobalShortcutsPortal against (a) a private session bus with no portal on it and (b) a
fake ``org.freedesktop.portal.Desktop`` implementing GlobalShortcuts + Request + Session,
which proves the request/response plumbing and the Activated → on_pressed wiring."""

import os
import threading
import time
import unittest
from unittest.mock import patch

from tests.harness import DisplaySession

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):   # pragma: no cover - depends on the box
    Gio = None

from gans.hotkeyspec import HotkeySpec  # noqa: E402

SPEC = HotkeySpec.DEFAULT

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.GlobalShortcuts"

PORTAL_XML = """
<node>
  <interface name="org.freedesktop.portal.GlobalShortcuts">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="BindShortcuts">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a(sa{sv})" name="shortcuts" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="request_handle" direction="out"/>
    </method>
    <signal name="Activated">
      <arg type="o" name="session_handle"/>
      <arg type="s" name="shortcut_id"/>
      <arg type="t" name="timestamp"/>
      <arg type="a{sv}" name="options"/>
    </signal>
    <property name="version" type="u" access="read"/>
  </interface>
</node>
"""

REQUEST_XML = """
<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg type="u" name="response"/>
      <arg type="a{sv}" name="results"/>
    </signal>
  </interface>
</node>
"""

SESSION_XML = """
<node>
  <interface name="org.freedesktop.portal.Session">
    <method name="Close"/>
  </interface>
</node>
"""


def pump(milliseconds=100):
    """Runs the default GLib main context (no GTK needed here)."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + milliseconds / 1000.0
    while time.monotonic() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(20)
    return bool(predicate())


def private_connection(address=None):
    """A connection of our own (not the process-wide singleton) that won't take the
    process down when the test bus goes away."""
    connection = Gio.DBusConnection.new_for_address_sync(
        address or os.environ["DBUS_SESSION_BUS_ADDRESS"],
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
        None, None)
    connection.set_exit_on_close(False)
    return connection


def mangle(unique_name):
    return unique_name.lstrip(":").replace(".", "_")


class FakePortal:
    """Just enough of xdg-desktop-portal's GlobalShortcuts to exercise a client. Like the
    real portal it lives outside the client's main loop — on its own thread with its own
    ``MainContext`` — so the client's synchronous calls can't deadlock on it."""

    def __init__(self, address, bind_response=0, trigger="Ctrl+Alt+Space", version=1, respond=True):
        self.bind_response = bind_response
        self.trigger = trigger
        self.version = version
        self.respond = respond
        self.bound = []
        self.sessions = []
        self.closed_sessions = []
        self._registrations = []
        self._ready = threading.Event()
        self._failure = None
        self._thread = threading.Thread(target=self._serve, args=(address,), name="fake-portal", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._failure is not None:
            raise self._failure

    def _serve(self, address):
        self.context = GLib.MainContext()
        self.context.push_thread_default()
        self.loop = GLib.MainLoop(self.context)
        try:
            self.connection = private_connection(address)
            self._register(OBJECT_PATH, PORTAL_XML, self._portal_call, self._get_property)
            reply = self.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                GLib.Variant("(su)", (BUS_NAME, 0)), GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            if reply.unpack()[0] != 1:
                raise AssertionError("couldn't own the portal name on the private bus")
        except Exception as error:   # surfaced to the test thread
            self._failure = error
            self._ready.set()
            return
        self._ready.set()
        self.loop.run()
        self.context.pop_thread_default()

    def _register(self, path, xml, handler, getter=None):
        info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
        self._registrations.append(self.connection.register_object(path, info, handler, getter, None))

    def stop(self):
        def shut_down(*_):
            for registration in self._registrations:
                self.connection.unregister_object(registration)
            self._registrations = []
            self.loop.quit()
            return False

        source = GLib.idle_source_new()   # runs inside the portal's loop, so quit() can't precede run()
        source.set_callback(shut_down)
        source.attach(self.context)
        self._thread.join(10)
        self.connection.close_sync(None)

    def _get_property(self, _connection, _sender, _path, _interface, name):
        return GLib.Variant("u", self.version) if name == "version" else None

    def _portal_call(self, _connection, sender, _path, _interface, method, parameters, invocation):
        if method == "CreateSession":
            (options,) = parameters.unpack()
            session_path = f"/org/freedesktop/portal/desktop/session/{mangle(sender)}/{options['session_handle_token']}"
            self._register(session_path, SESSION_XML, self._session_call)
            self.sessions.append(session_path)
            request_path = self._request(sender, options)
            invocation.return_value(GLib.Variant("(o)", (request_path,)))
            self._respond_later(sender, request_path, 0, {"session_handle": GLib.Variant("s", session_path)})
        elif method == "BindShortcuts":
            session, shortcuts, parent_window, options = parameters.unpack()
            self.bound.append((session, shortcuts, parent_window))
            request_path = self._request(sender, options)
            invocation.return_value(GLib.Variant("(o)", (request_path,)))
            results = {}
            if self.bind_response == 0:
                results["shortcuts"] = GLib.Variant("a(sa{sv})", [
                    (shortcut_id, {"description": GLib.Variant("s", opts.get("description", "")),
                                   "trigger_description": GLib.Variant("s", self.trigger)})
                    for shortcut_id, opts in shortcuts])
            self._respond_later(sender, request_path, self.bind_response, results)
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _request(self, sender, options):
        request_path = f"/org/freedesktop/portal/desktop/request/{mangle(sender)}/{options['handle_token']}"
        self._register(request_path, REQUEST_XML, self._request_call)
        return request_path

    def _request_call(self, _connection, _sender, _path, _interface, _method, _parameters, invocation):
        invocation.return_value(None)

    def _session_call(self, _connection, _sender, path, _interface, _method, _parameters, invocation):
        self.closed_sessions.append(path)
        invocation.return_value(None)

    def _respond_later(self, sender, request_path, code, results):
        """Emits the Request's Response — unicast to the caller, like the real portal. It
        is queued behind the method reply on the same connection, so it arrives after."""
        if self.respond:
            self.connection.emit_signal(sender, request_path, "org.freedesktop.portal.Request", "Response",
                                        GLib.Variant("(ua{sv})", (code, results)))

    def activate(self, shortcut_id, session=None):
        session = session or self.sessions[-1]
        self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, "Activated",
                                    GLib.Variant("(osta{sv})", (session, shortcut_id, int(time.time() * 1000), {})))


@unittest.skipUnless(Gio is not None, "PyGObject/Gio not available")
class GlobalShortcutsPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = DisplaySession.start()
        if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
            cls.session.stop()
            raise unittest.SkipTest("dbus-daemon is not available")
        from gans.platform.portal import GlobalShortcutsPortal
        cls.Portal = GlobalShortcutsPortal

    @classmethod
    def tearDownClass(cls):
        cls.session.stop()

    def setUp(self):
        self.presses = 0
        self.client_connection = private_connection()
        self.fake = None

    def tearDown(self):
        if self.fake is not None:
            self.fake.stop()
        self.client_connection.close_sync(None)

    def on_pressed(self):
        self.presses += 1

    def portal(self):
        return self.Portal(self.on_pressed, lambda fn: fn(), connection=self.client_connection)

    def start_fake(self, **kwargs):
        self.fake = FakePortal(os.environ["DBUS_SESSION_BUS_ADDRESS"], **kwargs)
        return self.fake

    def test_no_portal_on_the_bus_returns_false_quickly(self):
        portal = self.portal()
        started = time.monotonic()
        self.assertFalse(portal.bind(SPEC))
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertFalse(portal.is_bound)
        portal.close()   # harmless when nothing was bound
        self.assertEqual(self.presses, 0)

    def test_no_session_bus_returns_false(self):
        with patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/gans-test-bus"}):
            portal = self.Portal(self.on_pressed, lambda fn: fn())
            self.assertFalse(portal.bind(SPEC))
        self.assertFalse(portal.is_bound)

    def test_portal_without_the_interface_returns_false(self):
        self.start_fake(version=0)
        self.assertFalse(self.portal().bind(SPEC))
        self.assertEqual(self.fake.sessions, [])

    def test_bind_activation_and_close(self):
        fake = self.start_fake(trigger="Ctrl+Alt+Space")
        portal = self.portal()
        self.assertTrue(portal.bind(SPEC))
        self.assertTrue(portal.is_bound)
        self.assertEqual(portal.trigger_description, "Ctrl+Alt+Space")

        self.assertEqual(len(fake.sessions), 1)
        session, shortcuts, parent_window = fake.bound[0]
        self.assertEqual(session, fake.sessions[0])
        self.assertEqual(parent_window, "")
        self.assertEqual(shortcuts, [("quick-search", {"description": "Open Gans Quick Search",
                                                       "preferred_trigger": "CTRL+ALT+space"})])

        fake.activate("quick-search")
        self.assertTrue(wait_until(lambda: self.presses == 1))
        fake.activate("something-else")
        fake.activate("quick-search", session="/org/freedesktop/portal/desktop/session/x/other")
        pump(150)
        self.assertEqual(self.presses, 1)

        portal.close()
        self.assertFalse(portal.is_bound)
        self.assertIsNone(portal.trigger_description)
        self.assertTrue(wait_until(lambda: fake.closed_sessions == [session]))
        fake.activate("quick-search", session=session)
        pump(150)
        self.assertEqual(self.presses, 1)

    def test_rebind_replaces_the_session(self):
        fake = self.start_fake()
        portal = self.portal()
        self.assertTrue(portal.bind(SPEC))
        self.assertTrue(portal.bind(HotkeySpec(key="F12", super_=True)))
        self.assertEqual(len(fake.sessions), 2)
        self.assertTrue(wait_until(lambda: fake.closed_sessions == [fake.sessions[0]]))
        self.assertEqual(fake.bound[1][1][0][1]["preferred_trigger"], "LOGO+F12")
        fake.activate("quick-search")
        self.assertTrue(wait_until(lambda: self.presses == 1))

    def test_cancelled_bind_returns_false_and_closes_the_session(self):
        fake = self.start_fake(bind_response=1)
        portal = self.portal()
        self.assertFalse(portal.bind(SPEC))
        self.assertFalse(portal.is_bound)
        self.assertTrue(wait_until(lambda: fake.closed_sessions == fake.sessions))
        fake.activate("quick-search")
        pump(150)
        self.assertEqual(self.presses, 0)

    def test_unanswered_request_times_out(self):
        self.start_fake(respond=False)
        with patch.object(self.Portal, "RESPONSE_TIMEOUT", 0.3):
            portal = self.portal()
            started = time.monotonic()
            with self.assertLogs("gans.hotkey", level="WARNING"):
                self.assertFalse(portal.bind(SPEC))
            self.assertLess(time.monotonic() - started, 3.0)
        self.assertFalse(portal.is_bound)


if __name__ == "__main__":
    unittest.main()

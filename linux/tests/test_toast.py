"""Toasts as desktop notifications, headless: a fake application records what is sent,
withdrawn, and which actions are registered; the GLib main context is pumped by hand for
the expiry timers."""

import time
import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib  # noqa: E402

from gans.ui.toast import Toast  # noqa: E402


def pump(seconds: float) -> None:
    """Iterates the default main context (no GTK needed) for ``seconds``."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        pump(0.02)
    return bool(predicate())


class FakeApp:
    """Records the ``Gio.Application`` calls a toast makes."""

    def __init__(self, registered=True, fail_send=False):
        self.registered = registered
        self.fail_send = fail_send
        self.sent = []
        self.withdrawn = []
        self.actions = {}
        self.add_action_calls = 0

    def get_is_registered(self):
        return self.registered

    def send_notification(self, notification_id, notification):
        if self.fail_send:
            raise GLib.Error("no notification daemon")
        self.sent.append((notification_id, notification))

    def withdraw_notification(self, notification_id):
        self.withdrawn.append(notification_id)

    def add_action(self, action):
        self.add_action_calls += 1
        self.actions[action.get_name()] = action


def pending_action_id(toast: Toast) -> str:
    (action_id,) = toast._actions.keys()
    return action_id


class ToastTests(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        self.toast = Toast(self.app)

    def test_show_sends_one_replaceable_notification(self):
        self.toast.show("Code copied", duration=10)
        self.toast.show("Signed in", duration=10)
        self.assertEqual([item[0] for item in self.app.sent], ["gans-toast", "gans-toast"])
        for _, notification in self.app.sent:
            self.assertIsInstance(notification, Gio.Notification)
        self.assertEqual(self.app.withdrawn, [])
        self.assertEqual(self.app.add_action_calls, 0)   # no button, no action registered

    def test_toast_withdraws_itself_after_its_duration(self):
        self.toast.show("Code copied", duration=0.05)
        self.assertTrue(wait_for(lambda: self.app.withdrawn == ["gans-toast"]))
        pump(0.1)
        self.assertEqual(self.app.withdrawn, ["gans-toast"])   # withdrawn once, not repeatedly

    def test_replacement_is_not_withdrawn_by_the_earlier_timer(self):
        self.toast.show("first", duration=0.05)
        self.toast.show("second", duration=5)
        pump(0.3)   # the first toast's timer has fired by now
        self.assertEqual(self.app.withdrawn, [])
        self.toast.dismiss()
        self.assertEqual(self.app.withdrawn, ["gans-toast"])
        pump(0.1)
        self.assertEqual(self.app.withdrawn, ["gans-toast"])

    def test_action_button_runs_its_callback_once_then_dismisses(self):
        runs = []
        self.toast.show("Welcome", duration=10, action_title="Try it", action=lambda: runs.append("ran"))
        self.assertEqual(self.app.add_action_calls, 1)
        action = self.app.actions["toast"]
        self.assertIsInstance(action, Gio.SimpleAction)
        self.assertEqual(action.get_parameter_type().dup_string(), "s")

        action_id = pending_action_id(self.toast)
        action.activate(GLib.Variant("s", action_id))
        self.assertEqual(runs, ["ran"])
        self.assertEqual(self.app.withdrawn, ["gans-toast"])   # tapping the button dismisses the toast

        action.activate(GLib.Variant("s", action_id))         # consumed: a second tap is inert
        self.assertEqual(runs, ["ran"])
        self.assertEqual(self.toast._actions, {})

    def test_action_is_registered_once_for_every_toast(self):
        self.toast.show("one", duration=10, action_title="A", action=lambda: None)
        first_id = pending_action_id(self.toast)
        self.toast.show("two", duration=10, action_title="B", action=lambda: None)
        self.assertEqual(self.app.add_action_calls, 1)
        self.assertNotEqual(pending_action_id(self.toast), first_id)

    def test_replaced_toasts_button_is_stale(self):
        stale = []
        self.toast.show("one", duration=10, action_title="A", action=lambda: stale.append(1))
        stale_id = pending_action_id(self.toast)
        self.toast.show("two", duration=10)   # replaces the toast; the old button is gone with it
        with self.assertLogs("gans.app", level="DEBUG") as logs:
            self.app.actions["toast"].activate(GLib.Variant("s", stale_id))
        self.assertEqual(stale, [])
        self.assertIn("no longer pending", logs.output[0])
        self.assertEqual(self.app.withdrawn, [])

    def test_failing_action_is_logged_and_still_dismisses(self):
        def boom():
            raise RuntimeError("nope")
        self.toast.show("x", duration=10, action_title="Go", action=boom)
        with self.assertLogs("gans.app", level="ERROR"):
            self.app.actions["toast"].activate(GLib.Variant("s", pending_action_id(self.toast)))
        self.assertEqual(self.app.withdrawn, ["gans-toast"])

    def test_title_only_or_callback_only_means_no_button(self):
        self.toast.show("x", duration=10, action_title="Go")
        self.toast.show("y", duration=10, action=lambda: None)
        self.assertEqual(self.app.add_action_calls, 0)
        self.assertEqual(len(self.app.sent), 2)

    def test_expiry_drops_the_pending_action(self):
        self.toast.show("x", duration=0.05, action_title="Go", action=lambda: None)
        self.assertTrue(wait_for(lambda: self.app.withdrawn == ["gans-toast"]))
        self.assertEqual(self.toast._actions, {})


class ToastWithoutADaemonTests(unittest.TestCase):
    def test_no_application_is_a_logged_noop(self):
        toast = Toast(None)
        with self.assertLogs("gans.app", level="DEBUG") as logs:
            toast.show("Code copied", duration=0.01, action_title="Go", action=lambda: None)
        self.assertEqual(len(logs.output), 1)
        toast.dismiss()   # nothing to withdraw, nothing raised
        pump(0.05)

    def test_unregistered_application_is_skipped(self):
        app = FakeApp(registered=False)
        toast = Toast(app)
        with self.assertLogs("gans.app", level="DEBUG"):
            toast.show("Code copied", duration=0.01)
        pump(0.05)
        self.assertEqual(app.sent, [])
        self.assertEqual(app.withdrawn, [])

    def test_send_failure_is_a_logged_noop(self):
        app = FakeApp(fail_send=True)
        toast = Toast(app)
        with self.assertLogs("gans.app", level="DEBUG") as logs:
            toast.show("Code copied", duration=0.01, action_title="Go", action=lambda: None)
        self.assertIn("Couldn't show a notification", logs.output[-1])
        self.assertEqual(toast._actions, {})
        pump(0.05)
        self.assertEqual(app.withdrawn, [])   # no expiry timer was armed


if __name__ == "__main__":
    unittest.main()

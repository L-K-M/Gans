"""The update checker, headless: a fake GitHub client, a direct ``dispatch``, and the
presentation methods replaced with recorders. The real GTK dialogs get a separate smoke
test on the shared Xvfb display."""

import os
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from tests.harness import gtk_available

from gans.prefs import Preferences
from gans.updates import checker as checker_module
from gans.updates.checker import RESPONSE_DOWNLOAD, RESPONSE_SKIP, Configuration, UpdateChecker
from gans.updates.github import ClientError, GitHubRelease

INTERVAL = 3600.0


def release(tag: str, body="- Fixes\n- Features") -> GitHubRelease:
    return GitHubRelease(tag_name=tag, name=tag, body=body, html_url=f"https://github.com/L-K-M/Gans/releases/tag/{tag}",
                         prerelease=False, draft=False, published_at=None)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []
        self.gate = None   # a threading.Event the fetch waits on, to hold a check in flight

    def latest_release(self, include_prereleases):
        self.calls.append(include_prereleases)
        if self.gate is not None:
            self.gate.wait(5)
        if self.error is not None:
            raise self.error
        return self.result


class RecordingChecker(UpdateChecker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.available = []
        self.up_to_date = 0
        self.errors = []

    def present_update_available(self, release, remote, current):
        self.available.append((release.tag_name, str(remote), str(current)))

    def present_up_to_date(self):
        self.up_to_date += 1

    def present_error(self, error):
        self.errors.append(error)


def settle(checker: UpdateChecker, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while checker.is_checking and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not checker.is_checking, "check did not finish"


class UpdateCheckerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.prefs_path = Path(self.directory.name) / "preferences.json"
        self.prefs = Preferences(self.prefs_path)
        self.configuration = Configuration(owner="L-K-M", repo="Gans", app_name="Gans", current_version="1.2.0",
                                           allow_prereleases=False, minimum_check_interval=INTERVAL)

    def make(self, client, **overrides) -> RecordingChecker:
        configuration = Configuration(**{**self.configuration.__dict__, **overrides})
        return RecordingChecker(configuration, self.prefs, dispatch=lambda fn: fn(), client=client)

    # MARK: Configuration

    def test_configuration_defaults(self):
        configuration = Configuration(owner="o", repo="r", app_name="App", current_version="1.0")
        self.assertFalse(configuration.allow_prereleases)
        self.assertEqual(configuration.minimum_check_interval, 86_400.0)
        self.assertTrue(UpdateChecker(configuration, self.prefs, dispatch=lambda fn: fn()).automatic_checks_enabled)

    # MARK: check_now

    def test_check_now_reports_a_newer_version(self):
        client = FakeClient(release("v1.3.0"))
        checker = self.make(client)
        before = time.time()
        checker.check_now()
        settle(checker)
        self.assertEqual(client.calls, [False])
        self.assertEqual(checker.available, [("v1.3.0", "v1.3.0", "1.2.0")])
        self.assertEqual(checker.up_to_date, 0)
        self.assertGreaterEqual(checker.last_check_date, before)
        self.assertEqual(Preferences(self.prefs_path).update_last_check, checker.last_check_date)   # persisted

    def test_check_now_passes_the_prerelease_setting(self):
        client = FakeClient(release("v1.3.0-beta.1"))
        checker = self.make(client, allow_prereleases=True)
        checker.check_now()
        settle(checker)
        self.assertEqual(client.calls, [True])
        self.assertEqual(checker.available, [("v1.3.0-beta.1", "v1.3.0-beta.1", "1.2.0")])

    def test_check_now_reports_up_to_date(self):
        for tag in ("v1.2.0", "1.1.9", "v1.2.0-rc.1"):
            checker = self.make(FakeClient(release(tag)))
            checker.check_now()
            settle(checker)
            self.assertEqual(checker.available, [], tag)
            self.assertEqual(checker.up_to_date, 1, tag)

    def test_unparsable_versions_count_as_up_to_date(self):
        checker = self.make(FakeClient(release("nightly")))
        checker.check_now()
        settle(checker)
        self.assertEqual((checker.available, checker.up_to_date), ([], 1))
        self.assertIsNotNone(checker.last_check_date)   # the check itself succeeded

        checker = self.make(FakeClient(release("v9.0.0")), current_version="dev")
        checker.check_now()
        settle(checker)
        self.assertEqual((checker.available, checker.up_to_date), ([], 1))

    def test_check_now_reports_errors(self):
        error = ClientError("Network error: unreachable")
        checker = self.make(FakeClient(error=error))
        checker.check_now()
        settle(checker)
        self.assertEqual(checker.errors, [error])
        self.assertIsNone(checker.last_check_date)   # only successful checks count

    def test_unexpected_exceptions_are_reported_not_raised(self):
        checker = self.make(FakeClient(error=ValueError("bad json")))
        checker.check_now()
        settle(checker)
        self.assertEqual(len(checker.errors), 1)
        self.assertIsInstance(checker.errors[0], ValueError)

    def test_check_now_ignores_the_throttle_and_the_skipped_version(self):
        self.prefs.update_last_check = time.time()
        self.prefs.update_skipped_version = "v1.3.0"
        client = FakeClient(release("v1.3.0"))
        checker = self.make(client)
        checker.check_now()
        settle(checker)
        self.assertEqual(client.calls, [False])
        self.assertEqual(checker.available, [("v1.3.0", "v1.3.0", "1.2.0")])

    # MARK: check_in_background

    def test_background_check_is_silent_unless_newer_and_not_skipped(self):
        for tag, expected in (("v1.2.0", []), ("v1.3.0", [("v1.3.0", "v1.3.0", "1.2.0")])):
            self.prefs.update_last_check = None
            checker = self.make(FakeClient(release(tag)))
            checker.check_in_background()
            settle(checker)
            self.assertEqual(checker.available, expected, tag)
            self.assertEqual(checker.up_to_date, 0, tag)

        self.prefs.update_last_check = None
        self.prefs.update_skipped_version = "v1.3.0"
        checker = self.make(FakeClient(release("v1.3.0")))
        checker.check_in_background()
        settle(checker)
        self.assertEqual(checker.available, [])

        self.prefs.update_last_check = None
        checker = self.make(FakeClient(release("v1.4.0")))   # newer than the skipped one: offered again
        checker.check_in_background()
        settle(checker)
        self.assertEqual(checker.available, [("v1.4.0", "v1.4.0", "1.2.0")])

    def test_background_errors_are_logged_not_presented(self):
        checker = self.make(FakeClient(error=ClientError("GitHub returned HTTP 503.")))
        with self.assertLogs("gans.app", level="WARNING") as logs:
            checker.check_in_background()
            settle(checker)
        self.assertEqual(checker.errors, [])
        self.assertIn("HTTP 503", logs.output[0])

    def test_background_check_honours_the_throttle(self):
        client = FakeClient(release("v1.3.0"))
        self.prefs.update_last_check = time.time() - INTERVAL / 2
        checker = self.make(client)
        checker.check_in_background()
        settle(checker)
        self.assertEqual(client.calls, [])

        self.prefs.update_last_check = time.time() - INTERVAL * 2
        checker.check_in_background()
        settle(checker)
        self.assertEqual(client.calls, [False])

    def test_background_check_is_off_when_disabled(self):
        client = FakeClient(release("v1.3.0"))
        self.prefs.update_checks_enabled = False
        checker = self.make(client)
        checker.check_in_background()
        settle(checker)
        self.assertEqual(client.calls, [])
        self.assertFalse(checker.automatic_checks_enabled)

    # MARK: automatic_checks_enabled

    def test_enabling_automatic_checks_persists_and_checks(self):
        self.prefs.update_checks_enabled = False
        client = FakeClient(release("v1.3.0"))
        checker = self.make(client)
        changes = []
        checker.on_change(lambda: changes.append(checker.automatic_checks_enabled))

        checker.automatic_checks_enabled = True
        settle(checker)
        self.assertTrue(Preferences(self.prefs_path).update_checks_enabled)
        self.assertEqual(client.calls, [False])   # turning it on kicks off a (throttled) check
        self.assertTrue(changes[0])

        checker.automatic_checks_enabled = True    # already on: no extra check
        settle(checker)
        self.assertEqual(client.calls, [False])

        checker.automatic_checks_enabled = False
        self.assertFalse(Preferences(self.prefs_path).update_checks_enabled)
        self.assertFalse(changes[-1])

    # MARK: Concurrency / start

    def test_only_one_check_runs_at_a_time(self):
        client = FakeClient(release("v1.3.0"))
        client.gate = threading.Event()
        checker = self.make(client)
        states = []
        checker.on_change(lambda: states.append(checker.is_checking))
        checker.check_now()
        self.assertTrue(checker.is_checking)
        checker.check_now()
        checker.check_in_background()
        client.gate.set()
        settle(checker)
        self.assertEqual(client.calls, [False])
        self.assertEqual(checker.available, [("v1.3.0", "v1.3.0", "1.2.0")])
        self.assertEqual(states, [True, False])

    def test_start_is_a_noop_under_tests_or_when_opted_out(self):
        client = FakeClient(release("v1.3.0"))
        checker = self.make(client)
        with patch.object(checker_module.GLib, "timeout_add_seconds") as timer:
            checker.start()                                  # we're under unittest
            with patch.object(checker_module, "_running_under_tests", return_value=False), \
                    patch.dict(os.environ, {"GANS_NO_UPDATE_CHECK": "1"}):
                checker.start()
        settle(checker)
        self.assertEqual(client.calls, [])
        timer.assert_not_called()

    def test_start_checks_immediately_and_arms_the_daily_timer(self):
        client = FakeClient(release("v1.3.0"))
        checker = self.make(client)
        environment = {key: value for key, value in os.environ.items() if key != "GANS_NO_UPDATE_CHECK"}
        with patch.object(checker_module, "_running_under_tests", return_value=False), \
                patch.dict(os.environ, environment, clear=True), \
                patch.object(checker_module.GLib, "timeout_add_seconds", return_value=42) as timer:
            checker.start()
            settle(checker)
        self.assertEqual(client.calls, [False])
        self.assertEqual(checker.available, [("v1.3.0", "v1.3.0", "1.2.0")])
        timer.assert_called_once()
        interval, callback = timer.call_args[0]
        self.assertEqual(interval, int(INTERVAL))
        self.assertTrue(callback())                          # a repeating source
        settle(checker)
        self.assertEqual(client.calls, [False])              # …that respects the throttle


@unittest.skipUnless(gtk_available(), "PyGObject/GTK 3 not installed")
class UpdateDialogTests(unittest.TestCase):
    """The real dialogs: built non-blocking, with the right buttons, and their responses
    wired to the release page / the skipped version."""

    @classmethod
    def setUpClass(cls):
        from tests.gtkbind import gtk_session
        gtk_session()

    def setUp(self):
        from gi.repository import Gtk
        self.Gtk = Gtk
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.prefs = Preferences(Path(self.directory.name) / "preferences.json")
        self.checker = UpdateChecker(Configuration(owner="L-K-M", repo="Gans", app_name="Gans", current_version="1.2.0"),
                                     self.prefs, dispatch=lambda fn: fn(), client=FakeClient())
        self.opened = []
        self.checker._open_release_page = self.opened.append

    def dialog(self):
        from tests.harness import pump
        pump(50)
        dialogs = [window for window in self.Gtk.Window.list_toplevels()
                   if isinstance(window, self.Gtk.MessageDialog) and window.get_visible()]
        self.assertEqual(len(dialogs), 1)
        return dialogs[0]

    def button_labels(self, dialog):
        with warnings.catch_warnings():   # get_action_area is deprecated but the only order-preserving view
            warnings.simplefilter("ignore", DeprecationWarning)
            buttons = dialog.get_action_area().get_children()
        return [button.get_label() for button in buttons]

    def test_update_available_dialog(self):
        from gans.semver import SemanticVersion
        self.checker.present_update_available(release("v1.3.0"), SemanticVersion.parse("v1.3.0"),
                                              SemanticVersion.parse("1.2.0"))
        dialog = self.dialog()
        self.assertEqual(dialog.get_property("text"), "A new version of Gans is available")
        self.assertEqual(dialog.get_property("secondary-text"),
                         "Gans v1.3.0 is available — you have 1.2.0.\n\n- Fixes\n- Features")
        self.assertEqual(self.button_labels(dialog), ["Download", "Remind Me Later", "Skip This Version"])
        self.assertTrue(dialog.get_modal() is False)
        self.assertEqual(dialog.get_title(), "Gans")

        dialog.response(RESPONSE_SKIP)
        self.assertEqual(self.prefs.update_skipped_version, "v1.3.0")
        self.assertEqual(self.opened, [])
        self.assertNotIn(dialog, self.Gtk.Window.list_toplevels())   # destroyed on response

    def test_download_opens_the_release_page_only(self):
        from gans.semver import SemanticVersion
        self.checker.present_update_available(release("v1.3.0", body=""), SemanticVersion.parse("v1.3.0"),
                                              SemanticVersion.parse("1.2.0"))
        dialog = self.dialog()
        self.assertEqual(dialog.get_property("secondary-text"), "Gans v1.3.0 is available — you have 1.2.0.")
        dialog.response(RESPONSE_DOWNLOAD)
        self.assertEqual(self.opened, ["https://github.com/L-K-M/Gans/releases/tag/v1.3.0"])
        self.assertIsNone(self.prefs.update_skipped_version)

    def test_remind_me_later_changes_nothing(self):
        from gans.semver import SemanticVersion
        self.checker.present_update_available(release("v1.3.0"), SemanticVersion.parse("v1.3.0"),
                                              SemanticVersion.parse("1.2.0"))
        self.dialog().response(self.Gtk.ResponseType.CANCEL)
        self.assertEqual(self.opened, [])
        self.assertIsNone(self.prefs.update_skipped_version)

    def test_up_to_date_and_error_dialogs(self):
        self.checker.present_up_to_date()
        dialog = self.dialog()
        self.assertEqual(dialog.get_property("text"), "You're up to date")
        self.assertEqual(dialog.get_property("secondary-text"), "Gans 1.2.0 is the latest version.")
        self.assertEqual(self.button_labels(dialog), ["OK"])
        dialog.response(self.Gtk.ResponseType.OK)

        self.checker.present_error(ClientError("GitHub returned HTTP 503."))
        dialog = self.dialog()
        self.assertEqual(dialog.get_property("text"), "Couldn't check for updates")
        self.assertEqual(dialog.get_property("secondary-text"), "GitHub returned HTTP 503.")
        self.assertEqual(dialog.get_property("message-type"), self.Gtk.MessageType.WARNING)
        dialog.response(self.Gtk.ResponseType.OK)
        self.assertNotIn(dialog, self.Gtk.Window.list_toplevels())


if __name__ == "__main__":
    unittest.main()

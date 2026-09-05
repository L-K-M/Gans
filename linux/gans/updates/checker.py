"""Drop-in "is there a newer release on GitHub?" checker — the GTK half of the shared
``UpdateChecker`` (``gans.updates.github`` is the network half).

Configure it with a repository and call ``start()`` once at launch: it checks on startup
and then daily — throttled by a stored last-check date so relaunches don't spam — and
shows a dialog offering **Download**, **Remind Me Later**, or **Skip This Version** when a
newer release exists. ``check_now()`` is the user-initiated path (ignores the throttle
and also reports "you're up to date" and errors).

Its small state (enabled flag, skipped version, last-check date) lives in
``Preferences`` under the ``update*`` keys. The GitHub request runs on a worker thread
and every outcome is marshalled back through ``dispatch`` (``GLib.idle_add`` in the app),
so the presentation methods always run on the main loop; they're overridable, which is
also how the tests observe them without a display.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import log  # noqa: E402
from ..prefs import Preferences  # noqa: E402
from ..semver import SemanticVersion  # noqa: E402
from .github import GitHubRelease, GitHubReleaseClient  # noqa: E402

__all__ = ["Configuration", "UpdateChecker"]

Dispatch = Callable[[Callable[[], object]], object]

#: Custom dialog responses (positive ids are application-defined in GTK).
RESPONSE_DOWNLOAD = 1
RESPONSE_SKIP = 2


@dataclass
class Configuration:
    owner: str
    repo: str
    app_name: str
    current_version: str
    allow_prereleases: bool = False
    #: Seconds between automatic checks (and the background throttle).
    minimum_check_interval: float = 24 * 60 * 60.0


def _running_under_tests() -> bool:
    """Automatic checks stay off under a test runner, like the macOS build under XCTest."""
    return "unittest" in sys.modules or "pytest" in sys.modules


class UpdateChecker:
    def __init__(self, configuration: Configuration, prefs: Preferences, dispatch: Dispatch,
                 client: Optional[GitHubReleaseClient] = None):
        self.configuration = configuration
        self._prefs = prefs
        self._dispatch = dispatch
        self._client = client or GitHubReleaseClient(configuration.owner, configuration.repo)
        self._observers: List[Callable[[], None]] = []
        self._is_checking = False
        #: The outcome dialog currently on screen, if any. macOS runs its alert modally, so
        #: ``isChecking`` stays true until it's dismissed and a second check can't open a
        #: second alert; the GTK dialogs are non-blocking, so this remembers the open one.
        self._open_dialog: Optional[Gtk.Dialog] = None
        self._timer_id: Optional[int] = None

    # MARK: Observable state

    @property
    def automatic_checks_enabled(self) -> bool:
        """Whether to check automatically (on launch and daily). User-facing toggle; on
        unless the user has explicitly turned it off."""
        return self._prefs.update_checks_enabled

    @automatic_checks_enabled.setter
    def automatic_checks_enabled(self, enabled: bool) -> None:
        previous = self._prefs.update_checks_enabled
        self._prefs.update_checks_enabled = bool(enabled)
        self._notify()
        if enabled and not previous:
            self.check_in_background()

    @property
    def last_check_date(self) -> Optional[float]:
        """When the last successful check completed (epoch seconds), for a Settings "last
        checked" line."""
        return self._prefs.update_last_check

    @property
    def is_checking(self) -> bool:
        """True while a check is in flight or its outcome dialog is still on screen (to
        disable a "Check Now" button, say) — the lifetime of the macOS modal alert."""
        return self._is_checking

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        for callback in list(self._observers):
            try:
                callback()
            except Exception:
                log.app.exception("UpdateChecker observer failed")

    # MARK: Public API

    def start(self) -> None:
        """Begins automatic checking: an immediate (throttled) check plus a daily timer.
        Call once at launch. No-op under a test runner or with ``GANS_NO_UPDATE_CHECK``."""
        if os.environ.get("GANS_NO_UPDATE_CHECK") or _running_under_tests():
            log.app.debug("Automatic update checks are off for this process")
            return
        self.check_in_background()
        interval = max(1, int(round(self.configuration.minimum_check_interval)))
        self._timer_id = GLib.timeout_add_seconds(interval, self._on_timer)

    def _on_timer(self) -> bool:
        self.check_in_background()
        return True  # keep the repeating timer

    def check_in_background(self) -> None:
        """Runs a check only if automatic checks are on and the throttle interval has
        elapsed. Silent unless a newer, non-skipped version is found."""
        if not self.automatic_checks_enabled:
            return
        last = self.last_check_date
        if last is not None and time.time() - last < self.configuration.minimum_check_interval:
            return
        self._perform_check(user_initiated=False)

    def check_now(self) -> None:
        """User-initiated check (menu / Settings): ignores the throttle and always reports
        the outcome, including "you're up to date" and errors."""
        self._perform_check(user_initiated=True)

    # MARK: Check

    def _perform_check(self, user_initiated: bool) -> None:
        if self._is_checking:
            if user_initiated and self._open_dialog is not None:
                self._open_dialog.present()   # the outcome is already up: raise it, don't stack another
            return
        self._is_checking = True
        self._notify()
        allow_prereleases = self.configuration.allow_prereleases

        def fetch() -> None:
            try:
                release = self._client.latest_release(allow_prereleases)
            except Exception as error:  # the client's own errors, plus anything unexpected
                # Bind it: `error` is unbound once the except block exits, and with a real
                # dispatch (GLib.idle_add) the lambda runs after that.
                failure = error
                self._dispatch(lambda: self._finish(None, failure, user_initiated))
                return
            self._dispatch(lambda: self._finish(release, None, user_initiated))

        threading.Thread(target=fetch, name="gans-update-check", daemon=True).start()

    def _finish(self, release: Optional[GitHubRelease], error: Optional[Exception], user_initiated: bool) -> bool:
        """Main-loop half of a check. ``is_checking`` clears only after the outcome has been
        presented — and, when that outcome is a dialog, only once the dialog closes
        (``_on_dialog_destroyed``), so a repeat check can't stack a second one."""
        try:
            if error is not None or release is None:
                if user_initiated:
                    self.present_error(error if error is not None else RuntimeError("No release information."))
                else:
                    log.app.warning("Background update check failed: %s", error)
                return False
            self._prefs.update_last_check = time.time()

            remote = SemanticVersion.parse(release.tag_name)
            current = SemanticVersion.parse(self.configuration.current_version)
            if remote is None or current is None:
                # An unparsable tag or a dev build ("0.0.0-dev" parses; "dev" wouldn't):
                # nothing sensible to compare, so don't nag.
                if user_initiated:
                    self.present_up_to_date()
                return False
            if remote > current:
                if user_initiated or self._prefs.update_skipped_version != release.tag_name:
                    self.present_update_available(release, remote, current)
            elif user_initiated:
                self.present_up_to_date()
            return False
        finally:
            if self._open_dialog is None:
                self._is_checking = False
            self._notify()   # is_checking and/or last_check_date changed

    # MARK: Presentation (main thread; overridable)

    def present_update_available(self, release: GitHubRelease, remote: SemanticVersion,
                                 current: SemanticVersion) -> None:
        app = self.configuration.app_name
        info = f"{app} {remote} is available — you have {current}."
        notes = release.release_notes()
        if notes:
            info += f"\n\n{notes}"
        dialog = self._dialog(Gtk.MessageType.INFO, f"A new version of {app} is available", info)
        dialog.add_button("Download", RESPONSE_DOWNLOAD)
        dialog.add_button("Remind Me Later", Gtk.ResponseType.CANCEL)
        dialog.add_button("Skip This Version", RESPONSE_SKIP)
        dialog.set_default_response(RESPONSE_DOWNLOAD)
        dialog.connect("response", self._on_update_response, release)
        self._present(dialog)

    def _on_update_response(self, dialog: Gtk.Dialog, response: int, release: GitHubRelease) -> None:
        if response == RESPONSE_DOWNLOAD:
            # Open the release page in the browser so the user reviews and downloads the
            # update themselves. Gans never downloads, stores, or launches the binary.
            self._open_release_page(release.html_url)
        elif response == RESPONSE_SKIP:
            self._prefs.update_skipped_version = release.tag_name
        # Anything else is Remind Me Later (or the window closed) — re-offered next check.
        dialog.destroy()

    def _open_release_page(self, url: str) -> None:
        try:
            Gtk.show_uri_on_window(None, url, Gdk.CURRENT_TIME)
        except GLib.Error as error:
            log.app.error("Couldn't open the release page: %s", error.message)

    def present_up_to_date(self) -> None:
        configuration = self.configuration
        dialog = self._dialog(Gtk.MessageType.INFO, "You're up to date",
                              f"{configuration.app_name} {configuration.current_version} is the latest version.")
        dialog.add_button("OK", Gtk.ResponseType.OK)
        dialog.connect("response", lambda dialog, _response: dialog.destroy())
        self._present(dialog)

    def present_error(self, error: Exception) -> None:
        dialog = self._dialog(Gtk.MessageType.WARNING, "Couldn't check for updates", str(error))
        dialog.add_button("OK", Gtk.ResponseType.OK)
        dialog.connect("response", lambda dialog, _response: dialog.destroy())
        self._present(dialog)

    def _dialog(self, message_type: Gtk.MessageType, text: str, secondary: str) -> Gtk.MessageDialog:
        dialog = Gtk.MessageDialog(message_type=message_type, buttons=Gtk.ButtonsType.NONE, text=text)
        dialog.format_secondary_text(secondary)
        dialog.set_title(self.configuration.app_name)
        dialog.set_position(Gtk.WindowPosition.CENTER)
        # A tray agent has no window of its own to be in front of; without keep-above the
        # dialog can open behind whatever the user is working in, with no taskbar entry
        # to find it by.
        dialog.set_keep_above(True)
        return dialog

    def _present(self, dialog: Gtk.Dialog) -> None:
        """Non-blocking: the response signal handles the outcome and destroys the dialog,
        so the main loop (and the tray) keep running while it's up. The dialog is
        remembered (and ``is_checking`` held) until it's destroyed."""
        self._open_dialog = dialog
        dialog.connect("destroy", self._on_dialog_destroyed)
        dialog.show_all()
        dialog.present()

    def _on_dialog_destroyed(self, dialog: Gtk.Widget) -> None:
        if self._open_dialog is not dialog:
            return
        self._open_dialog = None
        self._is_checking = False
        self._notify()

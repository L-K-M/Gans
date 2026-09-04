"""Transient messages ("Code copied", permission hints, the first-run welcome) — the
``ToastPanel`` port. Fire-and-forget via ``Toast.show``.

On macOS the toast is a tiny floating HUD that fades out on its own. Here it's a desktop
notification (``Gio.Notification``): native on every desktop, placed by the shell (so it
never has to know where the pointer or Quick Search is), never steals focus from the app
the user is returning to, and supports an action button ("Try it", "Grant…"). One fixed
notification id means a new toast replaces the previous one, exactly as the panel did,
and the toast withdraws itself after ``duration`` so it doesn't pile up in the shell's
notification list. Without an application (or a notification daemon) it's a logged no-op —
a toast must never break the action it confirms.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib  # noqa: E402

from .. import log  # noqa: E402

__all__ = ["Toast"]

Action = Callable[[], object]


class Toast:
    #: A single id: sending again replaces the visible toast instead of stacking.
    NOTIFICATION_ID = "gans-toast"
    #: The app-scope action a toast button invokes, with the toast's id as its target.
    ACTION_NAME = "toast"
    DETAILED_ACTION = "app.toast"
    ICON_NAME = "ch.lkmc.Gans"
    TITLE = "Gans"

    def __init__(self, app):
        """``app`` is the ``Gio.Application`` to notify through (duck-typed:
        ``send_notification`` / ``withdraw_notification`` / ``add_action``); ``None``
        disables toasts."""
        self._app = app
        #: Bumped per show/dismiss so an earlier toast's expiry can't withdraw a newer one.
        self._generation = 0
        self._action_installed = False
        self._action_counter = 0
        #: Pending button callbacks by toast id; invoking one removes it.
        self._actions: Dict[str, Action] = {}

    # MARK: Show / dismiss

    def show(self, message: str, duration: float = 2.4,
             action_title: Optional[str] = None, action: Optional[Action] = None) -> None:
        """Shows ``message`` for ``duration`` seconds, replacing any toast still visible.
        With ``action_title`` and ``action`` the notification carries a button that runs
        ``action`` (once) and dismisses the toast."""
        self._generation += 1
        generation = self._generation
        self._actions.clear()  # the previous toast (and its button) is being replaced
        if self._app is None:
            log.app.debug("No application to notify through; toast dropped")
            return
        is_registered = getattr(self._app, "get_is_registered", None)
        if callable(is_registered) and not is_registered():
            log.app.debug("Application not registered yet; toast dropped")
            return

        notification = Gio.Notification.new(self.TITLE)
        notification.set_body(message)
        notification.set_icon(Gio.ThemedIcon.new(self.ICON_NAME))
        if action_title and action is not None:
            self._install_action()
            self._action_counter += 1
            action_id = f"toast-{self._action_counter}"
            self._actions[action_id] = action
            notification.add_button_with_target(action_title, self.DETAILED_ACTION, GLib.Variant("s", action_id))

        try:
            self._app.send_notification(self.NOTIFICATION_ID, notification)
        except GLib.Error as error:
            log.app.debug("Couldn't show a notification: %s", error.message)
            self._actions.clear()
            return
        GLib.timeout_add(max(1, int(duration * 1000)), self._expire, generation)

    def dismiss(self) -> None:
        """Dismisses the current toast now (used by an action button once tapped)."""
        self._generation += 1
        self._actions.clear()
        self._withdraw()

    def _expire(self, generation: int) -> bool:
        if generation == self._generation:
            self._actions.clear()
            self._withdraw()
        return False  # one-shot GLib source

    def _withdraw(self) -> None:
        if self._app is None:
            return
        try:
            self._app.withdraw_notification(self.NOTIFICATION_ID)
        except GLib.Error as error:
            log.app.debug("Couldn't withdraw the notification: %s", error.message)

    # MARK: Action button

    def _install_action(self) -> None:
        """Registers the one ``app.toast(s)`` action the first time a button is needed."""
        if self._action_installed:
            return
        action = Gio.SimpleAction.new(self.ACTION_NAME, GLib.VariantType.new("s"))
        action.connect("activate", self._on_action_activated)
        self._app.add_action(action)
        self._action_installed = True

    def _on_action_activated(self, _action, parameter) -> None:
        action_id = parameter.get_string() if parameter is not None else ""
        callback = self._actions.pop(action_id, None)
        if callback is None:
            log.app.debug("Toast action %r is no longer pending", action_id)
            return
        try:
            callback()
        except Exception:
            log.app.exception("Toast action failed")
        self.dismiss()

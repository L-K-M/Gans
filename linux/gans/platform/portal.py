"""The XDG ``GlobalShortcuts`` portal — the Wayland-era way for an app to ask the desktop
for a global key. KDE Plasma (and any other desktop whose portal backend implements it)
handles this; GNOME's backend doesn't, which is why GNOME gets the custom keybinding.

Protocol (https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.GlobalShortcuts.html):

1. ``CreateSession`` → a Request object whose ``Response`` signal carries the session
   handle.
2. ``BindShortcuts(session, [(id, {description, preferred_trigger})])`` → another
   Request/Response. The desktop may show a consent dialog and may choose a trigger
   other than the preferred one; it reports what it bound as ``trigger_description``.
3. Presses arrive as the ``Activated`` signal on the portal object.

Requests are answered asynchronously, so ``bind`` runs a nested main loop until each
``Response`` arrives (or 30 s pass — the consent dialog needs a human). Every error,
timeout, or non-zero response code makes ``bind`` return ``False`` so ``HotkeyManager``
can fall through to the next backend. The nested loop keeps dispatching the app's own
events, so ``close`` may be called *while* ``bind`` waits: it then closes the pending
Request (the desktop drops its dialog) and that ``bind`` returns ``False``.
"""

from __future__ import annotations

import itertools
import os
from typing import Callable, Dict, Optional, Tuple

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .. import log  # noqa: E402
from ..hotkeyspec import HotkeySpec  # noqa: E402

__all__ = ["GlobalShortcutsPortal"]

Dispatch = Callable[[Callable[[], object]], object]

_tokens = itertools.count(1)


class GlobalShortcutsPortal:
    BUS_NAME = "org.freedesktop.portal.Desktop"
    OBJECT_PATH = "/org/freedesktop/portal/desktop"
    INTERFACE = "org.freedesktop.portal.GlobalShortcuts"
    REQUEST_INTERFACE = "org.freedesktop.portal.Request"
    SESSION_INTERFACE = "org.freedesktop.portal.Session"
    SHORTCUT_ID = "quick-search"
    DESCRIPTION = "Open Gans Quick Search"
    #: How long to wait for each Request's Response (the desktop may show a consent dialog).
    RESPONSE_TIMEOUT = 30.0
    #: Timeout for the method calls themselves; they return the request handle immediately.
    CALL_TIMEOUT_MS = 10_000

    def __init__(self, on_pressed: Callable[[], object], dispatch: Dispatch,
                 connection: Optional[Gio.DBusConnection] = None):
        """``connection`` defaults to the shared session bus; tests pass a private one."""
        self._on_pressed = on_pressed
        self._dispatch = dispatch
        self._connection = connection
        self._session_handle: Optional[str] = None
        self._activated_subscription: Optional[int] = None
        #: ``(request_path, loop, outcome)`` of the Request a ``bind`` is waiting on.
        self._pending: Optional[Tuple[str, GLib.MainLoop, Dict[str, object]]] = None
        #: Identifies the current ``bind`` so one abandoned from a nested main loop can't
        #: tear down the newer one that replaced it.
        self._attempt: Optional[object] = None
        #: The desktop's description of the trigger it actually bound (may differ from
        #: the preferred one), for the Settings status line.
        self.trigger_description: Optional[str] = None

    @property
    def is_bound(self) -> bool:
        return self._session_handle is not None

    # MARK: Bind / close

    def bind(self, spec: HotkeySpec) -> bool:
        """Creates a portal session and binds the shortcut. False on any failure — including
        ``close`` being called while this waits for the desktop's answer."""
        self.close()
        attempt = self._attempt = object()
        try:
            if self._bind(spec):
                return True
        except GLib.Error as error:
            log.hotkey.info("GlobalShortcuts portal request failed: %s", error.message)
        except Exception:
            log.hotkey.exception("GlobalShortcuts portal binding failed")
        if self._attempt is attempt:   # not superseded by a newer bind() on this object
            self.close()
        return False

    def close(self) -> None:
        """Stops listening and closes the portal session (fire-and-forget). A ``bind`` still
        waiting for the desktop is abandoned: its Request is closed, which ends the consent
        dialog, and it returns False once its nested loop unwinds."""
        connection = self._connection
        pending, self._pending = self._pending, None
        if pending is not None and connection is not None:
            request_path, loop, outcome = pending
            outcome["cancelled"] = True
            connection.call(self.BUS_NAME, request_path, self.REQUEST_INTERFACE, "Close", None, None,
                            Gio.DBusCallFlags.NONE, self.CALL_TIMEOUT_MS, None, self._on_close_reply)
            loop.quit()
        if self._activated_subscription is not None and connection is not None:
            connection.signal_unsubscribe(self._activated_subscription)
        self._activated_subscription = None
        session_handle, self._session_handle = self._session_handle, None
        if session_handle is not None and connection is not None:
            connection.call(self.BUS_NAME, session_handle, self.SESSION_INTERFACE, "Close", None, None,
                            Gio.DBusCallFlags.NONE, self.CALL_TIMEOUT_MS, None, self._on_close_reply)
        self.trigger_description = None

    # MARK: Protocol

    def _bind(self, spec: HotkeySpec) -> bool:
        connection = self._connect()
        if connection is None:
            return False
        if self._interface_version(connection) < 1:
            return False
        self._connection = connection
        # Request/Session object paths embed the caller's unique name, mangled as the spec says.
        sender = (connection.get_unique_name() or "").lstrip(":").replace(".", "_")

        session_token = self._token()
        # Known up front (the spec fixes the path) so close() can still close the session
        # when the wait for the CreateSession response is abandoned or times out.
        self._session_handle = f"/org/freedesktop/portal/desktop/session/{sender}/{session_token}"
        code, results = self._request(
            connection, sender, "CreateSession", "(a{sv})", (),
            {"session_handle_token": GLib.Variant("s", session_token)})
        if code != 0:
            return False
        handle = results.get("session_handle")
        if handle:
            self._session_handle = str(handle)

        # Listen before binding so an early activation can't slip past.
        self._activated_subscription = connection.signal_subscribe(
            self.BUS_NAME, self.INTERFACE, "Activated", self.OBJECT_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_activated)

        shortcuts = [(self.SHORTCUT_ID, {
            "description": GLib.Variant("s", self.DESCRIPTION),
            "preferred_trigger": GLib.Variant("s", spec.portal_trigger),
        })]
        code, results = self._request(
            connection, sender, "BindShortcuts", "(oa(sa{sv})sa{sv})",
            (self._session_handle, shortcuts, ""), {})
        if code != 0:
            return False
        self.trigger_description = self._bound_trigger(results)
        log.hotkey.info("GlobalShortcuts portal bound %s as %s", spec.display_string,
                        self.trigger_description or "(unspecified trigger)")
        return True

    def _connect(self) -> Optional[Gio.DBusConnection]:
        if self._connection is not None:
            return self._connection
        try:
            return Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            log.hotkey.info("No session bus for the GlobalShortcuts portal: %s", error.message)
            return None

    def _interface_version(self, connection: Gio.DBusConnection) -> int:
        """The interface's ``version`` property; 0 when the portal (or this interface)
        isn't there, which is the quick negative answer on GNOME and headless boxes."""
        try:
            reply = connection.call_sync(
                self.BUS_NAME, self.OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (self.INTERFACE, "version")), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, self.CALL_TIMEOUT_MS, None)
        except GLib.Error as error:
            log.hotkey.info("GlobalShortcuts portal not available: %s", error.message)
            return 0
        version = reply.unpack()[0]
        return int(version) if isinstance(version, int) else 0

    @staticmethod
    def _token() -> str:
        return f"gans{os.getpid()}_{next(_tokens)}"

    def _request(self, connection: Gio.DBusConnection, sender: str, method: str, signature: str,
                 leading: Tuple, options: Dict[str, GLib.Variant]) -> Tuple[int, Dict[str, object]]:
        """Calls a portal method that answers through a Request object and waits (nested
        main loop) for its ``Response``. Returns ``(code, results)``; a timeout, or ``close``
        abandoning the wait, counts as code 2 ("other"). Subscribing *before* the call is
        essential: the Response can be emitted before the method reply is even read."""
        token = self._token()
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        parameters = GLib.Variant(signature, leading + (options,))

        loop = GLib.MainLoop()
        outcome: Dict[str, object] = {}

        def on_response(_connection, _sender, _path, _interface, _signal, params) -> None:
            outcome["code"], outcome["results"] = params.unpack()
            loop.quit()

        def on_timeout() -> bool:
            outcome["timed_out"] = True
            loop.quit()
            return False

        subscription = connection.signal_subscribe(
            self.BUS_NAME, self.REQUEST_INTERFACE, "Response", request_path, None,
            Gio.DBusSignalFlags.NONE, on_response)
        self._pending = (request_path, loop, outcome)
        try:
            connection.call_sync(self.BUS_NAME, self.OBJECT_PATH, self.INTERFACE, method, parameters,
                                 GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, self.CALL_TIMEOUT_MS, None)
            timeout = GLib.timeout_add(int(self.RESPONSE_TIMEOUT * 1000), on_timeout)
            loop.run()
            if "timed_out" not in outcome:
                GLib.source_remove(timeout)
        finally:
            if self._pending is not None and self._pending[1] is loop:
                self._pending = None
            connection.signal_unsubscribe(subscription)
        if "cancelled" in outcome:
            log.hotkey.info("GlobalShortcuts %s abandoned: closed before the desktop answered", method)
            return 2, {}
        if "code" not in outcome:
            log.hotkey.warning("GlobalShortcuts %s: no response within %.0f s", method, self.RESPONSE_TIMEOUT)
            return 2, {}
        code = int(outcome["code"])
        if code != 0:
            log.hotkey.info("GlobalShortcuts %s answered %s (1 = cancelled by the user)", method, code)
        results = outcome["results"]
        return code, results if isinstance(results, dict) else {}

    def _bound_trigger(self, results: Dict[str, object]) -> Optional[str]:
        shortcuts = results.get("shortcuts")
        if not isinstance(shortcuts, list):
            return None
        for item in shortcuts:
            if isinstance(item, (tuple, list)) and len(item) == 2 and item[0] == self.SHORTCUT_ID:
                description = item[1].get("trigger_description") if isinstance(item[1], dict) else None
                return description if isinstance(description, str) and description else None
        return None

    # MARK: Signals

    def _on_activated(self, _connection, _sender, _path, _interface, _signal, parameters) -> None:
        try:
            session_handle, shortcut_id, _timestamp, _options = parameters.unpack()
        except (TypeError, ValueError):
            return
        if session_handle != self._session_handle or shortcut_id != self.SHORTCUT_ID:
            return

        def fire() -> bool:
            self._on_pressed()
            return False   # so GLib.idle_add doesn't repeat

        self._dispatch(fire)

    def _on_close_reply(self, connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        """Reply to a Session/Request ``Close``; both are fire-and-forget."""
        try:
            connection.call_finish(result)
        except GLib.Error as error:
            log.hotkey.debug("Closing a GlobalShortcuts object failed: %s", error.message)

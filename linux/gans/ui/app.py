"""Boots and wires the app: initializes crypto, installs the tray item and global hotkey,
restores the Ente session, and coordinates the windows — the ``AppDelegate`` port.

``Gtk.Application`` with the ``ch.lkmc.Gans`` id gives us single-instance behavior for
free: a second ``gans toggle`` / ``gans settings`` / ``gans quit`` invocation is forwarded
to this process over D-Bus and handled in ``do_command_line``. That is also how a desktop
keyboard shortcut (GNOME custom shortcut → ``gans toggle``) reaches Quick Search.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

from .. import crypto, log  # noqa: E402
from ..ente.api import EnteAPI  # noqa: E402
from ..ente.vault import EnteVault  # noqa: E402
from ..platform.applock import AppLock  # noqa: E402
from ..platform.clipboard import Clipboard  # noqa: E402
from ..platform.honk import Honk  # noqa: E402
from ..platform.hotkey import HotkeyManager  # noqa: E402
from ..platform.inject import CodeInjector  # noqa: E402
from ..platform.x11 import X11Session  # noqa: E402
from ..prefs import Preferences  # noqa: E402
from ..store.cache import EntityCache  # noqa: E402
from ..store.keyring import open_keyring  # noqa: E402
from ..updates.checker import Configuration, UpdateChecker  # noqa: E402
from ..version import app_version  # noqa: E402
from .css import install_css  # noqa: E402
from .login import LoginWindowController  # noqa: E402
from .quicksearch import QuickSearchController  # noqa: E402
from .settings import SettingsWindowController  # noqa: E402
from .toast import Toast  # noqa: E402
from .tray import StatusItemController  # noqa: E402

__all__ = ["GansApplication"]


def _dispatch(fn: Callable[[], object]) -> None:
    """Runs ``fn`` on the GTK main loop exactly once (idle callbacks repeat when they
    return a truthy value, so the return value is swallowed)."""
    GLib.idle_add(lambda: (fn(), False)[1])


class GansApplication(Gtk.Application):
    APP_ID = "ch.lkmc.Gans"

    #: Panel-open / wake refreshes are throttled to this; the timer refreshes anyway.
    AUTO_REFRESH_THROTTLE = 60.0
    AUTO_REFRESH_INTERVAL = 15 * 60

    def __init__(self) -> None:
        super().__init__(application_id=self.APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self._booted = False
        self._last_auto_refresh = 0.0
        self._sleep_subscription: Optional[int] = None
        self._system_bus = None

    # MARK: GApplication lifecycle

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        # A tray agent has no window to keep the main loop alive; hold it explicitly.
        self.hold()
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_terminate, None)
        self._boot()

    def do_activate(self) -> None:
        # Reached via D-Bus Activate (e.g. a DBusActivatable launcher); plain `gans` runs are
        # delivered to do_command_line instead, thanks to HANDLES_COMMAND_LINE.
        if self._booted:
            self.quick_search.show()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        args = list(command_line.get_arguments())[1:]
        if not self._booted:
            return 1  # crypto failed to initialize; do_startup already logged it
        command = args[0] if args else ""
        if command == "toggle":
            self.quick_search.toggle()
        elif command == "search":
            self.quick_search.show()
        elif command == "settings":
            self.show_settings()
        elif command == "quit":
            self.quit()
        elif command.startswith("ente-cli://"):
            # The passkey flow redirects the browser to `ente-cli://passkey` when the
            # ceremony finishes. We register that scheme purely so the redirect brings
            # Gans forward; the token is still retrieved by polling, so there's nothing
            # to parse from the URL.
            self.login_window.show()
        elif command_line.get_is_remote():
            # A second plain `gans` (launcher/desktop icon click) opens Quick Search.
            self.quick_search.show()
        return 0

    def do_shutdown(self) -> None:
        if self._booted:
            self.hotkey.unregister()
            self.quick_search.hide(restore_focus=False)
            self.x11.close()
        Gtk.Application.do_shutdown(self)

    def _on_terminate(self, *_args) -> bool:
        self.quit()
        return False

    # MARK: Boot

    def _boot(self) -> None:
        if not crypto.initialize():
            log.app.critical("libsodium failed to initialize")
            self.quit()
            return
        install_css()

        self.prefs = Preferences()
        self.api = EnteAPI()
        self.login_api = EnteAPI()
        # The real keyring arrives in _start_session (opening it may prompt); until then
        # the vault is signed out and Quick Search reports "still starting".
        self.vault = EnteVault(self.api, None, EntityCache(), dispatch=_dispatch)
        self._session_started = False
        self.app_lock = AppLock(self.prefs, dispatch=_dispatch)
        self.x11 = X11Session()
        self.clipboard = Clipboard()
        self.injector = CodeInjector(self.clipboard, self.x11)
        self.toast = Toast(self)
        self.update_checker = UpdateChecker(
            Configuration(owner="L-K-M", repo="Gans", app_name="Gans", current_version=app_version()),
            self.prefs, dispatch=_dispatch)

        self.quick_search = QuickSearchController(self.prefs, self.injector, self.x11, app=self)
        self.login_window = LoginWindowController(self.vault, self.login_api, app=self)
        self.hotkey = HotkeyManager(on_pressed=self.quick_search.toggle, dispatch=_dispatch, x11=self.x11)
        self.settings_window = SettingsWindowController(self.prefs, self.vault, self.update_checker, self.hotkey,
                                                        self.app_lock, self.injector, app=self)
        self.tray = StatusItemController(
            self.vault, self.prefs, self.app_lock,
            on_quick_search=self.quick_search.show,
            on_settings=self.show_settings,
            on_login=self.login_window.show,
            on_check_for_updates=self.update_checker.check_now,
            on_unlock=self._prompt_unlock,
            on_quit=self.quit,
            clipboard=self.clipboard,
            honk=Honk,
        )
        self._booted = True

        self._wire_quick_search()
        self._wire_settings()
        self._register_hotkey()

        self.update_checker.start()
        self._observe_vault()
        self._start_auto_refresh()

        self._start_session()
        self._present_onboarding_if_needed()

    # MARK: Wiring

    def _wire_quick_search(self) -> None:
        def entries_provider():
            # Opening Quick Search is the moment freshness matters: kick off a
            # (throttled) background sync while the current entries show immediately —
            # _observe_vault() live-updates the open panel if anything changed.
            self._refresh_if_stale()
            return self.vault.entries

        self.quick_search.entries_provider = entries_provider
        self.quick_search.is_signed_in = lambda: self.vault.is_signed_in
        self.quick_search.on_needs_login = self.login_window.show
        self.quick_search.is_locked = lambda: not self._session_started or self.app_lock.is_locked
        self.quick_search.on_locked = self._on_quick_search_locked
        # A Quick Search commit gets the same confirmation as a menu copy (glyph blink,
        # and the honk in honk mode) — the tray item owns the glyph.
        self.quick_search.on_committed = self.tray.confirm_copy

    def _wire_settings(self) -> None:
        self.settings_window.on_sign_in = self.login_window.show
        self.settings_window.on_hotkey_changed = self._register_hotkey

    def _register_hotkey(self) -> None:
        status = self.hotkey.register(self.prefs.hotkey)
        if status.ok:
            log.hotkey.info("Hotkey %s via %s: %s", self.prefs.hotkey.display_string, status.backend, status.detail)
        else:
            log.hotkey.warning("No global hotkey backend: %s", status.detail)

    # MARK: Session

    def _start_session(self) -> None:
        """Opens the keyring on a worker thread — the Secret Service may put up an unlock
        or create-keyring prompt, which must never freeze the tray — then continues in
        ``_session_ready``."""
        def resolve() -> None:
            keyring = open_keyring()
            _dispatch(lambda: self._session_ready(keyring))
        self._run_in_thread(resolve)

    def _session_ready(self, keyring) -> None:
        """With the keyring in hand: if the app lock is on and a session exists, start
        locked and prompt for unlock before touching the token; otherwise restore the
        session (and open sign-in if nobody's signed in)."""
        self.keyring = keyring
        self.vault.adopt_keyring(keyring)
        self._session_started = True
        if self.app_lock.is_enabled and self.vault.is_signed_in:
            self.app_lock.lock_if_enabled()
            self._prompt_unlock()
        else:
            def restore():
                self.vault.restore()
                _dispatch(self._after_restore)
            self._run_in_thread(restore)

    def _after_restore(self) -> None:
        if not self.vault.is_signed_in:
            self.login_window.show()

    def _on_quick_search_locked(self) -> None:
        if not self._session_started:
            self.toast.show("Gans is still starting up — one moment.", duration=3)
            return
        self._prompt_unlock()

    def _prompt_unlock(self) -> None:
        """Asks for the user's password (polkit); on success, restores the (until-now
        untouched) session."""
        def completed(success: bool) -> None:
            if success:
                self._run_in_thread(self.vault.restore)
        self.app_lock.authenticate(completion=completed)

    # MARK: Auto refresh

    def _start_auto_refresh(self) -> None:
        """New codes added on other devices should appear without a manual "Refresh Now":
        sync periodically, on resume from sleep, and when Quick Search opens — all funneled
        through a shared throttle."""
        GLib.timeout_add_seconds(self.AUTO_REFRESH_INTERVAL, self._on_refresh_timer)
        # The 15-minute timer doesn't fire while the machine sleeps; catch up on resume
        # via logind's PrepareForSleep(false).
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._sleep_subscription = self._system_bus.signal_subscribe(
                "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
                "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep)
        except GLib.Error as error:
            log.app.debug("No logind sleep signal available: %s", error.message)

    def _on_refresh_timer(self) -> bool:
        self._refresh_if_stale(ignore_throttle=True)
        return True

    def _on_prepare_for_sleep(self, _connection, _sender, _path, _iface, _signal, parameters) -> None:
        going_to_sleep = parameters.unpack()[0] if parameters.n_children() else False
        if not going_to_sleep:
            self._refresh_if_stale()

    def _refresh_if_stale(self, ignore_throttle: bool = False) -> None:
        if not self.vault.is_signed_in or self.app_lock.is_locked:
            return
        if not ignore_throttle and time.monotonic() - self._last_auto_refresh <= self.AUTO_REFRESH_THROTTLE:
            return
        self._last_auto_refresh = time.monotonic()
        self._run_in_thread(self.vault.refresh)

    def _observe_vault(self) -> None:
        """Keep the open Quick Search panel's list fresh when a sync lands."""
        def changed() -> None:
            if self.quick_search.is_visible:
                self.quick_search.model.set_entries(self.vault.entries)
        self.vault.on_change(changed)

    # MARK: Onboarding

    def _present_onboarding_if_needed(self) -> None:
        """First launch only: a gentle welcome that names the hotkey and offers to open
        Quick Search — then never shown again."""
        if self.prefs.has_completed_onboarding:
            return
        self.prefs.has_completed_onboarding = True
        status = self.hotkey.status
        if status is not None and status.ok:
            message = (f"Gans lives in your system tray. Press {self.prefs.hotkey.display_string} anywhere to "
                       "search your 2FA codes and type them into whatever app you're in.")
        else:
            message = ("Gans lives in your system tray. Bind a keyboard shortcut to “gans toggle” to search "
                       "your 2FA codes from anywhere and type them into whatever app you're in.")

        def show() -> bool:
            self.toast.show(message, duration=12, action_title="Try it", action=self.quick_search.show)
            return False
        GLib.timeout_add(1500, show)

    # MARK: Shared services for the UI

    def show_settings(self) -> None:
        self.settings_window.show()

    def present_after_login(self) -> None:
        """Called once a session is fully established — e.g. after the browser passkey
        flow left the browser frontmost."""
        email = self.vault.account_email or "Ente"
        self.toast.show(f"Signed in as {email}. Press {self.prefs.hotkey.display_string} to search your codes.",
                        duration=5)

    @staticmethod
    def _run_in_thread(fn: Callable[[], object]) -> threading.Thread:
        def guarded() -> None:
            try:
                fn()
            except Exception:
                log.app.exception("Background task failed")
        thread = threading.Thread(target=guarded, daemon=True)
        thread.start()
        return thread

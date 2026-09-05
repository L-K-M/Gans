"""The Settings window — the port of ``SettingsView.swift``, ``HotkeyRecorderView.swift``
and ``SettingsWindowController.swift``: account, most-used codes, Quick Search behavior,
security, typing status, startup, and updates, laid out as a grouped form (a bold
section title over a framed list of rows).

The rows are built once and refreshed **in place** whenever the preferences, the vault,
or the update checker change, so the window never flickers or loses scroll position.
Programmatic switch/combo updates are guarded so they don't echo back into the
preferences as user edits.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from .. import log  # noqa: E402
from ..hotkeyspec import HotkeySpec  # noqa: E402
from ..prefs import DeliveryMode, Preferences  # noqa: E402

try:
    from .css import install_css
except ImportError:  # the stylesheet is optional: the window is plain GTK without it
    install_css = None

__all__ = ["HotkeyRecorder", "SettingsWindowController", "SettingsWindow"]

WINDOW_WIDTH = 460
MIN_HEIGHT = 520
DEFAULT_HEIGHT = 640

RECORDING_TEXT = "Press keys…"

#: Modifier keys on their own never complete a chord (the user is still holding them).
_MODIFIER_KEYVALS = frozenset({
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R,
    Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R,
    Gdk.KEY_ISO_Level3_Shift, Gdk.KEY_ISO_Level5_Shift, Gdk.KEY_Mode_switch,
    Gdk.KEY_Caps_Lock, Gdk.KEY_Shift_Lock, Gdk.KEY_Num_Lock, Gdk.KEY_Scroll_Lock,
})

#: "Clear after" choices: label → seconds.
_CLEAR_DELAYS: Tuple[Tuple[str, int], ...] = (("15 seconds", 15), ("30 seconds", 30), ("1 minute", 60),
                                             ("2 minutes", 120))

UNLOCK_CAPTION = ("When on, Gans locks on launch (and via “Lock Now”) and asks for your password through the "
                  "system’s authentication dialog before showing or typing any codes. The Ente token and key "
                  "stay in your keyring.")
TYPING_AVAILABLE = "Typing into other apps: available (X11)"
TYPING_UNAVAILABLE = "Typing into other apps: unavailable — codes are copied to the clipboard instead"
TYPING_CAPTION = ("Automatic typing and pasting require a native X11 session with XTest. "
                  "On Wayland, codes are copied instead; paste with Ctrl+V. XWayland cannot reliably "
                  "identify the focused native window. Changing GANS_GDK_BACKEND does not enable typing.")
NO_KEYRING_WARNING = "No keyring available — Gans will ask you to sign in again after it quits."


def _format_time(timestamp: float) -> str:
    """Local time, abbreviated date + short time (``Sep 4, 2026, 21:54``)."""
    return time.strftime("%b %-d, %Y, %H:%M", time.localtime(timestamp))


# MARK: Hotkey recorder

class HotkeyRecorder(Gtk.Button):
    """A button that records the next key-with-modifiers chord and reports it as a
    ``HotkeySpec``. Click it, then press the desired combination; Esc cancels. Only a
    chord with at least one modifier is accepted, so the hotkey can't swallow ordinary
    typing (a bare key beeps and keeps recording).

    While recording, the chord is captured on the **toplevel** rather than the button:
    ``Gtk.Window``'s own key handler runs mnemonics and accelerators before it forwards
    the key to the focused widget, so a chord that doubles as a window shortcut would
    otherwise trigger that shortcut instead of being recorded (the ``performKeyEquivalent``
    override in the macOS recorder exists for the same reason)."""

    def __init__(self, spec: HotkeySpec, on_capture: Callable[[HotkeySpec], None]) -> None:
        super().__init__()
        self._spec = spec
        self._on_capture = on_capture
        self._recording = False
        self._toplevel_handler: Optional[Tuple[Gtk.Widget, int]] = None
        self.set_size_request(160, -1)
        self.get_style_context().add_class("gans-hotkey-recorder")
        self.connect("clicked", self._on_clicked)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("unmap", self._on_unmap)
        self._refresh_label()

    @property
    def spec(self) -> HotkeySpec:
        return self._spec

    @spec.setter
    def spec(self, value: HotkeySpec) -> None:
        self._spec = value
        self._refresh_label()

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _refresh_label(self) -> None:
        self.set_label(RECORDING_TEXT if self._recording else self._spec.display_string)

    # MARK: Recording

    def _on_clicked(self, _button: Gtk.Button) -> None:
        if self._recording:
            return
        toplevel = self.get_toplevel()
        if not isinstance(toplevel, Gtk.Window):
            return
        self._recording = True
        self._toplevel_handler = (toplevel, toplevel.connect("key-press-event", self._on_key_press))
        self.grab_focus()
        self._refresh_label()

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        handler, self._toplevel_handler = self._toplevel_handler, None
        if handler is not None:
            widget, handler_id = handler
            widget.disconnect(handler_id)
        self._refresh_label()

    def _on_focus_out(self, _widget: Gtk.Widget, _event: Gdk.EventFocus) -> bool:
        self._stop_recording()  # like resigning first responder on macOS
        return False

    def _on_unmap(self, _widget: Gtk.Widget) -> None:
        self._stop_recording()

    def _on_key_press(self, _window: Gtk.Widget, event: Gdk.EventKey) -> bool:
        if not self._recording:
            return False
        if event.keyval == Gdk.KEY_Escape:
            self._stop_recording()  # Esc cancels (it can't be a sensible bare hotkey anyway)
            return True
        if event.keyval in _MODIFIER_KEYVALS:
            return True  # still holding modifiers; wait for the key
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        spec = HotkeySpec(
            key=self._key_name(event),
            control=bool(state & Gdk.ModifierType.CONTROL_MASK),
            alt=bool(state & Gdk.ModifierType.MOD1_MASK),
            shift=bool(state & Gdk.ModifierType.SHIFT_MASK),
            super_=bool(state & (Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.MOD4_MASK)),
        )
        if not spec.has_modifier or not spec.key:
            Gdk.beep()  # require at least one modifier; keep recording
            return True
        self._spec = spec
        self._stop_recording()
        self._on_capture(spec)
        return True

    def _key_name(self, event: Gdk.EventKey) -> str:
        """The key's *unmodified* name in the current layout (``1`` for Shift+1, ``a``
        for Shift+A) — the form GNOME's custom shortcuts and X11 grabs expect."""
        keyval = event.keyval
        keymap = Gdk.Keymap.get_for_display(self.get_display())
        translated = keymap.translate_keyboard_state(event.hardware_keycode, Gdk.ModifierType(0), event.group)
        if translated[0] and translated[1]:
            keyval = translated[1]
        return Gdk.keyval_name(Gdk.keyval_to_lower(keyval)) or ""


# MARK: Form building blocks

class _Section:
    """A bold title over a framed ``Gtk.ListBox`` of rows — the grouped-form look."""

    def __init__(self, title: str) -> None:
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.widget.get_style_context().add_class("gans-settings-section")
        label = Gtk.Label()
        label.set_markup(f"<b>{title}</b>")
        label.set_xalign(0)
        self.widget.pack_start(label, False, False, 0)
        frame = Gtk.Frame()
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        frame.add(self.list)
        self.widget.pack_start(frame, False, False, 0)

    def add_row(self, content: Gtk.Widget) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.get_style_context().add_class("gans-settings-row")
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        row.add(content)
        self.list.add(row)
        return row

    def add_labeled(self, title: str, control: Gtk.Widget) -> Gtk.ListBoxRow:
        """"Title ............ control" — a label on the left, the control on the right."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        label = Gtk.Label(label=title)
        label.set_xalign(0)
        label.set_line_wrap(True)
        box.pack_start(label, True, True, 0)
        control.set_valign(Gtk.Align.CENTER)
        box.pack_end(control, False, False, 0)
        return self.add_row(box)

    def add_value(self, title: str) -> Tuple[Gtk.ListBoxRow, Gtk.Label]:
        """A read-only "Title ... value" row; returns the value label for refreshing."""
        value = Gtk.Label()
        value.set_xalign(1)
        value.set_selectable(True)
        value.get_style_context().add_class("dim-label")
        return self.add_labeled(title, value), value

    def add_caption(self, text: str) -> Tuple[Gtk.ListBoxRow, Gtk.Label]:
        """A wrapped, dim explanatory line."""
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_max_width_chars(56)
        label.get_style_context().add_class("dim-label")
        label.get_style_context().add_class("gans-settings-caption")
        return self.add_row(label), label

    def add_button(self, title: str, on_click: Callable[[], object],
                   style_class: Optional[str] = None) -> Tuple[Gtk.ListBoxRow, Gtk.Button]:
        button = Gtk.Button(label=title)
        if style_class:
            button.get_style_context().add_class(style_class)
        button.connect("clicked", lambda _button: on_click())
        button.set_halign(Gtk.Align.START)
        return self.add_row(button), button

    def clear(self) -> None:
        for row in self.list.get_children():
            self.list.remove(row)


# MARK: Window

class SettingsWindow(Gtk.Window):
    """The form. Every collaborator is duck-typed so the window can be built around stubs;
    ``refresh()`` re-reads all of them."""

    def __init__(self, prefs: Preferences, vault: object, update_checker: object, hotkey_manager: object,
                 app_lock: object, injector: object, launch_at_login: object,
                 on_sign_in: Callable[[], object], on_hotkey_changed: Callable[[], object]) -> None:
        super().__init__(title="Gans Settings")
        self._prefs = prefs
        self._vault = vault
        self._update_checker = update_checker
        self._hotkey_manager = hotkey_manager
        self._app_lock = app_lock
        self._injector = injector
        self._launch_at_login = launch_at_login
        self._on_sign_in = on_sign_in
        self._on_hotkey_changed = on_hotkey_changed
        self._refreshing = False

        self.set_default_size(WINDOW_WIDTH, DEFAULT_HEIGHT)
        self.set_size_request(WINDOW_WIDTH, MIN_HEIGHT)
        self.set_position(Gtk.WindowPosition.CENTER)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.add(scroller)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self._content.set_border_width(20)
        scroller.add(self._content)

        self._build_account()
        self._build_most_used()
        self._build_quick_search()
        self._build_security()
        self._build_typing()
        self._build_startup()
        self._build_updates()

        # Show everything once; from here on visibility is managed piecewise by refresh()
        # (the window itself is shown with show()/present(), never show_all()).
        scroller.show_all()
        # The user may bind the hotkey elsewhere, start an X server, or grant something
        # in another app and switch back; re-read the world on every focus-in.
        self.connect("focus-in-event", self._on_focus_in)
        self.refresh()

    # MARK: Sections

    def _add_section(self, title: str) -> _Section:
        section = _Section(title)
        self._content.pack_start(section.widget, False, False, 0)
        return section

    def _build_account(self) -> None:
        section = self._add_section("Account")
        self.signed_in_row, self.signed_in_label = section.add_value("Signed in")
        self.last_sync_row, self.last_sync_label = section.add_value("Last sync")
        self.entries_row, self.entries_label = section.add_value("Entries")
        self.sign_out_row, self.sign_out_button = section.add_button("Sign Out", self._sign_out, "destructive-action")
        self.sign_in_row, self.sign_in_button = section.add_button("Sign in to Ente…", self._on_sign_in)

        warning = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic", Gtk.IconSize.MENU)
        icon.set_valign(Gtk.Align.START)
        warning.pack_start(icon, False, False, 0)
        self.keyring_warning_label = Gtk.Label(label=NO_KEYRING_WARNING)
        self.keyring_warning_label.set_xalign(0)
        self.keyring_warning_label.set_line_wrap(True)
        self.keyring_warning_label.set_max_width_chars(52)
        warning.pack_start(self.keyring_warning_label, True, True, 0)
        self.keyring_warning_row = section.add_row(warning)

    def _build_most_used(self) -> None:
        self._most_used = self._add_section("Most used")
        self.most_used_section = self._most_used.widget

    def _build_quick_search(self) -> None:
        section = self._add_section("Quick Search")
        self.hotkey_recorder = HotkeyRecorder(self._prefs.hotkey, self._hotkey_captured)
        section.add_labeled("Hotkey", self.hotkey_recorder)
        self.hotkey_status_row, self.hotkey_status_label = section.add_caption("")

        self.delivery_combo = Gtk.ComboBoxText()
        for mode in DeliveryMode:
            self.delivery_combo.append(mode.value, mode.label)
        self.delivery_combo.connect("changed", self._delivery_changed)
        section.add_labeled("On select", self.delivery_combo)

        self.show_codes_switch = self._switch(lambda active: setattr(self._prefs, "show_codes_in_quick_search", active))
        section.add_labeled("Show codes in Quick Search", self.show_codes_switch)

        self.also_copy_switch = self._switch(lambda active: setattr(self._prefs, "also_copy_when_typing", active))
        self.also_copy_row = section.add_labeled("Also copy to clipboard", self.also_copy_switch)

        self.clear_clipboard_switch = self._switch(
            lambda active: setattr(self._prefs, "clear_clipboard_enabled", active))
        section.add_labeled("Clear copied codes from the clipboard", self.clear_clipboard_switch)

        self.clear_after_combo = Gtk.ComboBoxText()
        for label, seconds in _CLEAR_DELAYS:
            self.clear_after_combo.append(str(seconds), label)
        self.clear_after_combo.connect("changed", self._clear_after_changed)
        self.clear_after_row = section.add_labeled("Clear after", self.clear_after_combo)

        self.honk_switch = self._switch(lambda active: setattr(self._prefs, "honk_on_copy", active))
        section.add_labeled("Honk on copy 🪿", self.honk_switch)

    def _build_security(self) -> None:
        section = self._add_section("Security")
        self.require_unlock_switch = self._switch(lambda active: setattr(self._prefs, "require_unlock", active))
        section.add_labeled("Require your password to unlock", self.require_unlock_switch)
        section.add_caption(UNLOCK_CAPTION)
        _row, self.lock_now_button = section.add_button("Lock Now", self._lock_now)

    def _build_typing(self) -> None:
        section = self._add_section("Typing")
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.typing_icon = Gtk.Image()
        self.typing_icon.set_valign(Gtk.Align.START)
        status.pack_start(self.typing_icon, False, False, 0)
        self.typing_label = Gtk.Label()
        self.typing_label.set_xalign(0)
        self.typing_label.set_line_wrap(True)
        self.typing_label.set_max_width_chars(52)
        status.pack_start(self.typing_label, True, True, 0)
        section.add_row(status)
        section.add_caption(TYPING_CAPTION)

    def _build_startup(self) -> None:
        section = self._add_section("Startup")
        self.launch_at_login_switch = self._switch(self._launch_at_login.set)
        section.add_labeled("Launch Gans at login", self.launch_at_login_switch)

    def _build_updates(self) -> None:
        section = self._add_section("Updates")
        self.auto_update_switch = self._switch(
            lambda active: setattr(self._update_checker, "automatic_checks_enabled", active))
        section.add_labeled("Check for updates automatically", self.auto_update_switch)
        self.last_checked_row, self.last_checked_label = section.add_value("Last checked")
        _row, self.check_now_button = section.add_button("Check Now", self._update_checker.check_now)

    def _switch(self, on_toggle: Callable[[bool], object]) -> Gtk.Switch:
        switch = Gtk.Switch()

        def toggled(widget: Gtk.Switch, _param: object) -> None:
            if not self._refreshing:
                on_toggle(widget.get_active())
        switch.connect("notify::active", toggled)
        return switch

    # MARK: Refresh

    def refresh(self) -> None:
        """Re-reads every collaborator into the rows. Guarded so the programmatic switch
        and combo updates don't echo back into the preferences (whose change notification
        would otherwise re-enter here)."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            self._refresh_account()
            self._refresh_most_used()
            self._refresh_quick_search()
            self._refresh_security()
            self._refresh_typing()
            self._refresh_startup()
            self._refresh_updates()
        finally:
            self._refreshing = False

    def _refresh_account(self) -> None:
        vault = self._vault
        signed_in = bool(vault.is_signed_in)
        self.signed_in_row.set_visible(signed_in)
        self.entries_row.set_visible(signed_in)
        self.sign_out_row.set_visible(signed_in)
        self.sign_in_row.set_visible(not signed_in)
        last_sync = vault.last_sync
        self.last_sync_row.set_visible(signed_in and last_sync is not None)
        if signed_in:
            self.signed_in_label.set_text(vault.account_email or "Ente")
            self.entries_label.set_text(str(len(vault.entries)))
            if last_sync is not None:
                self.last_sync_label.set_text(_format_time(last_sync))
        self.keyring_warning_row.set_visible(not bool(getattr(vault, "keyring_persistent", True)))

    def _refresh_most_used(self) -> None:
        """Top entries by use count — a small "you reach for these" summary. Hidden when
        nothing has been used yet (or the used entries are gone)."""
        by_id = {}
        for entry in self._vault.entries:
            by_id.setdefault(entry.id, entry)
        rows = [(by_id[entry_id], count) for entry_id, count in self._prefs.most_used(5) if entry_id in by_id]
        self._most_used.clear()
        for entry, count in rows:
            row, label = self._most_used.add_value(entry.display_name)
            label.set_text(f"{count}×")
            row.show_all()
        self.most_used_section.set_visible(bool(rows))

    @property
    def most_used_texts(self) -> List[Tuple[str, str]]:
        """``[(display name, "N×")]`` as shown — for tests."""
        result = []
        for row in self._most_used.list.get_children():
            box = row.get_child()
            name, value = box.get_children()
            result.append((name.get_text(), value.get_text()))
        return result

    def _refresh_quick_search(self) -> None:
        prefs = self._prefs
        if not self.hotkey_recorder.is_recording:
            self.hotkey_recorder.spec = prefs.hotkey
        self.hotkey_status_label.set_text(self._hotkey_status_text())
        self.delivery_combo.set_active_id(prefs.delivery_mode.value)
        self.show_codes_switch.set_active(prefs.show_codes_in_quick_search)
        self.also_copy_switch.set_active(prefs.also_copy_when_typing)
        self.also_copy_row.set_visible(prefs.delivery_mode is DeliveryMode.TYPE)
        self.clear_clipboard_switch.set_active(prefs.clear_clipboard_enabled)
        if self.clear_after_combo.set_active_id(str(prefs.clear_clipboard_seconds)) is False:
            self.clear_after_combo.set_active_id("30")  # a value not in the menu shows the default
        self.clear_after_row.set_visible(prefs.clear_clipboard_enabled)
        self.honk_switch.set_active(prefs.honk_on_copy)

    def _hotkey_status_text(self) -> str:
        status = getattr(self._hotkey_manager, "status", None)
        detail = getattr(status, "detail", "") if status is not None else ""
        if detail:
            return detail
        manual = getattr(self._hotkey_manager, "manual_instructions", None)
        return manual() if callable(manual) else ""

    def _refresh_security(self) -> None:
        self.require_unlock_switch.set_active(self._prefs.require_unlock)

    def _refresh_typing(self) -> None:
        available = bool(self._injector.can_inject)
        self.typing_icon.set_from_icon_name("object-select-symbolic" if available else "dialog-warning-symbolic",
                                            Gtk.IconSize.MENU)
        self.typing_label.set_text(TYPING_AVAILABLE if available else TYPING_UNAVAILABLE)

    def _refresh_startup(self) -> None:
        self.launch_at_login_switch.set_active(bool(self._launch_at_login.is_enabled()))

    def _refresh_updates(self) -> None:
        checker = self._update_checker
        self.auto_update_switch.set_active(bool(checker.automatic_checks_enabled))
        last = checker.last_check_date
        self.last_checked_row.set_visible(last is not None)
        if last is not None:
            self.last_checked_label.set_text(_format_time(last))
        self.check_now_button.set_sensitive(not bool(checker.is_checking))

    # MARK: Handlers

    def _hotkey_captured(self, spec: HotkeySpec) -> None:
        self._prefs.hotkey = spec
        self._on_hotkey_changed()
        # Re-registration just happened; show how it went.
        self.hotkey_status_label.set_text(self._hotkey_status_text())

    def _delivery_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._refreshing:
            return
        mode_id = combo.get_active_id()
        if mode_id is not None:
            self._prefs.delivery_mode = DeliveryMode(mode_id)

    def _clear_after_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._refreshing:
            return
        seconds = combo.get_active_id()
        if seconds is not None:
            self._prefs.clear_clipboard_seconds = int(seconds)

    def _sign_out(self) -> None:
        self._vault.sign_out()

    def _lock_now(self) -> None:
        self._app_lock.lock()

    def _on_focus_in(self, _widget: Gtk.Widget, _event: Gdk.EventFocus) -> bool:
        self.refresh()
        return False


# MARK: Controller

class SettingsWindowController:
    """Owns the single Settings window: builds it on first ``show()``, subscribes it to
    the preferences / vault / update checker, hides it on close, and re-presents it on
    every later ``show()``."""

    def __init__(self, prefs: Preferences, vault: object, update_checker: object, hotkey_manager: object,
                 app_lock: object, injector: object, app: Optional[object] = None,
                 launch_at_login: Optional[object] = None) -> None:
        self._prefs = prefs
        self._vault = vault
        self._update_checker = update_checker
        self._hotkey_manager = hotkey_manager
        self._app_lock = app_lock
        self._injector = injector
        self._app = app
        self._launch_at_login = launch_at_login
        self.window: Optional[SettingsWindow] = None
        #: Opens the login window.
        self.on_sign_in: Callable[[], object] = lambda: None
        #: The hotkey preference changed; re-register it.
        self.on_hotkey_changed: Callable[[], object] = lambda: None

    def show(self) -> None:
        if self.window is None:
            self.window = self._build()
        else:
            self.window.refresh()
        self.window.show()
        self.window.present()

    def close(self) -> None:
        if self.window is not None:
            self.window.hide()

    def _build(self) -> SettingsWindow:
        if install_css is not None:
            install_css()
        launch_at_login = self._launch_at_login
        if launch_at_login is None:
            from ..platform.autostart import LaunchAtLogin
            launch_at_login = LaunchAtLogin
        window = SettingsWindow(self._prefs, self._vault, self._update_checker, self._hotkey_manager,
                                self._app_lock, self._injector, launch_at_login,
                                on_sign_in=lambda: self.on_sign_in(),
                                on_hotkey_changed=lambda: self.on_hotkey_changed())
        app = self._app
        if app is not None and hasattr(app, "add_window"):
            app.add_window(window)
        window.connect("delete-event", self._on_delete)
        for source in (self._prefs, self._vault, self._update_checker):
            subscribe = getattr(source, "on_change", None)
            if callable(subscribe):
                subscribe(window.refresh)
            else:
                log.app.debug("%s has no on_change; Settings won't live-update from it", type(source).__name__)
        return window

    def _on_delete(self, window: Gtk.Window, _event: Gdk.Event) -> bool:
        window.hide()
        return True  # keep the window for the next show()

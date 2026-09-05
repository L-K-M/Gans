"""The login window — the port of ``LoginView.swift`` + ``LoginWindowController.swift``.

Walks through credentials → optional email code / 2FA / passkey → done. Email + password
are always collected because the password unwraps the key hierarchy regardless of how
the session is authenticated. ``LoginWindow`` renders the ``LoginViewModel``; the
controller creates it once, hides it instead of destroying it on close, and re-presents
it on every ``show()``.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import log  # noqa: E402
from ..ente.api import EnteAPI  # noqa: E402
from ..ente.login import EnteLogin  # noqa: E402
from ..ente.vault import EnteVault  # noqa: E402
from .login_model import LoginViewModel  # noqa: E402

try:
    from .css import install_css
except ImportError:  # the stylesheet is optional: the window is plain GTK without it
    install_css = None

__all__ = ["LoginWindowController", "LoginWindow"]

Dispatch = Callable[[Callable[[], object]], object]

CONTENT_WIDTH = 380
PADDING = 24

PAGE_CREDENTIALS = "credentials"
PAGE_CODE = "code"
PAGE_PASSKEY = "passkey"


def _dispatch(fn: Callable[[], object]) -> None:
    """Runs ``fn`` on the GTK main loop exactly once."""
    GLib.idle_add(lambda: (fn(), False)[1])


# MARK: Widgets

class _BusyButton(Gtk.Button):
    """A button whose label gives way to a spinner while an action is in flight (the
    SwiftUI ``if model.isBusy { ProgressView() } else { Text(...) }``)."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        self._spinner = Gtk.Spinner()
        self._spinner.set_no_show_all(True)
        self._label = Gtk.Label(label=title)
        box.pack_start(self._spinner, False, False, 0)
        box.pack_start(self._label, False, False, 0)
        self.add(box)
        self.set_can_default(True)

    @property
    def title(self) -> str:
        return self._label.get_text()

    @property
    def is_spinning(self) -> bool:
        return self._spinner.get_visible()

    def set_busy(self, busy: bool, busy_title: Optional[str] = None) -> None:
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()
        self._label.set_text(busy_title if (busy and busy_title) else self._title)


class LoginWindow(Gtk.Window):
    """Renders a ``LoginViewModel``: a header, one page per stage in a ``Gtk.Stack``, and
    an error line. The entries write straight into the model; everything else is
    re-rendered from it on ``on_change``."""

    def __init__(self, model: LoginViewModel) -> None:
        super().__init__(title="Sign in to Ente")
        self._model = model
        self._rendering = False
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.set_border_width(PADDING)
        root.set_size_request(CONTENT_WIDTH, -1)
        self.add(root)

        root.pack_start(self._build_header(), False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_homogeneous(False)
        self.stack.add_named(self._build_credentials(), PAGE_CREDENTIALS)
        self.stack.add_named(self._build_code_entry(), PAGE_CODE)
        self.stack.add_named(self._build_passkey(), PAGE_PASSKEY)
        root.pack_start(self.stack, False, False, 0)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_max_width_chars(48)
        self.error_label.set_no_show_all(True)
        self.error_label.get_style_context().add_class("gans-error")
        root.pack_start(self.error_label, False, False, 0)

        # Show everything once; from here on visibility is managed piecewise by render()
        # (the window itself is shown with show()/present(), never show_all()).
        root.show_all()
        model.on_change(self.render)
        self.render()

    # MARK: Building

    @staticmethod
    def _build_header() -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.DND)
        icon.set_pixel_size(28)
        box.pack_start(icon, False, False, 0)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label()
        title.set_markup("<b>Sign in to Ente</b>")
        title.set_xalign(0)
        subtitle = Gtk.Label(label="Your codes stay end-to-end encrypted.")
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("dim-label")
        text.pack_start(title, False, False, 0)
        text.pack_start(subtitle, False, False, 0)
        box.pack_start(text, True, True, 0)
        return box

    def _build_credentials(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        self.email_entry = Gtk.Entry()
        self.email_entry.set_placeholder_text("Email")
        self.email_entry.set_input_purpose(Gtk.InputPurpose.EMAIL)
        self.email_entry.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
        self.email_entry.set_activates_default(True)
        self.email_entry.connect("changed", self._on_email_changed)
        page.pack_start(self.email_entry, False, False, 0)

        self.password_entry = Gtk.Entry()
        self.password_entry.set_placeholder_text("Password")
        self.password_entry.set_visibility(False)
        self.password_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_activates_default(True)
        self.password_entry.connect("changed", self._on_password_changed)
        page.pack_start(self.password_entry, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sign_in_button = _BusyButton("Sign In")
        self.sign_in_button.get_style_context().add_class("suggested-action")
        self.sign_in_button.connect("clicked", self._on_sign_in)
        buttons.pack_start(self.sign_in_button, False, False, 0)
        self.email_code_button = Gtk.Button(label="Email me a code")
        self.email_code_button.connect("clicked", self._on_email_code)
        buttons.pack_start(self.email_code_button, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    def _build_code_entry(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.code_title = Gtk.Label()
        self.code_title.set_xalign(0)
        page.pack_start(self.code_title, False, False, 0)
        self.code_help = Gtk.Label()
        self.code_help.set_xalign(0)
        self.code_help.set_line_wrap(True)
        self.code_help.get_style_context().add_class("dim-label")
        page.pack_start(self.code_help, False, False, 0)

        self.code_entry = Gtk.Entry()
        self.code_entry.set_placeholder_text("Code")
        self.code_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self.code_entry.set_activates_default(True)
        self.code_entry.connect("changed", self._on_code_changed)
        page.pack_start(self.code_entry, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.continue_button = _BusyButton("Continue")
        self.continue_button.get_style_context().add_class("suggested-action")
        self.continue_button.connect("clicked", self._on_continue)
        buttons.pack_start(self.continue_button, False, False, 0)
        self.back_button = Gtk.Button(label="Back")
        self.back_button.connect("clicked", self._on_restart)
        buttons.pack_start(self.back_button, False, False, 0)
        page.pack_start(buttons, False, False, 0)
        return page

    def _build_passkey(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        title = Gtk.Label(label="This account uses a passkey")
        title.set_xalign(0)
        page.pack_start(title, False, False, 0)
        explanation = Gtk.Label(label=(
            "Authenticate with your passkey in the browser. Gans finishes signing you in "
            "automatically once you're done — just come back to this window."))
        explanation.set_xalign(0)
        explanation.set_line_wrap(True)
        explanation.set_max_width_chars(48)
        explanation.get_style_context().add_class("dim-label")
        page.pack_start(explanation, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.passkey_button = _BusyButton("Authenticate with Passkey")
        self.passkey_button.get_style_context().add_class("suggested-action")
        self.passkey_button.connect("clicked", self._on_passkey)
        buttons.pack_start(self.passkey_button, False, False, 0)
        self.passkey_cancel_button = Gtk.Button(label="Back")
        self.passkey_cancel_button.connect("clicked", self._on_restart)
        buttons.pack_start(self.passkey_cancel_button, False, False, 0)
        page.pack_start(buttons, False, False, 0)

        self.passkey_waiting_label = Gtk.Label(label=(
            "Waiting for you to finish in your browser. If the page didn't open, click again."))
        self.passkey_waiting_label.set_xalign(0)
        self.passkey_waiting_label.set_line_wrap(True)
        self.passkey_waiting_label.set_max_width_chars(48)
        self.passkey_waiting_label.get_style_context().add_class("dim-label")
        self.passkey_waiting_label.set_no_show_all(True)
        page.pack_start(self.passkey_waiting_label, False, False, 0)
        return page

    # MARK: Rendering

    def render(self) -> None:
        """Reflects the model. Guarded so the entry updates it makes don't echo back into
        the model as user edits."""
        model = self._model
        self._rendering = True
        try:
            self._set_entry_text(self.email_entry, model.email)
            self._set_entry_text(self.password_entry, model.password)
            self._set_entry_text(self.code_entry, model.code)

            stage = model.stage
            if stage.is_credentials:
                page, default = PAGE_CREDENTIALS, self.sign_in_button
            elif stage.is_passkey:
                page, default = PAGE_PASSKEY, self.passkey_button
            else:
                page, default = PAGE_CODE, self.continue_button
                if stage.is_email_code:
                    self.code_title.set_text("Enter the code we emailed you")
                    self.code_help.set_text(f"Check {model.email}.")
                else:
                    self.code_title.set_text("Two-factor code")
                    self.code_help.set_text("Enter the 6-digit code from your authenticator.")
            self.stack.set_visible_child_name(page)
            self.set_default(default)

            can_submit = model.can_submit_credentials
            self.sign_in_button.set_sensitive(can_submit)
            self.sign_in_button.set_busy(model.is_busy)
            self.email_code_button.set_sensitive(can_submit)

            self.continue_button.set_sensitive(bool(model.code) and not model.is_busy)
            self.continue_button.set_busy(model.is_busy)

            self.passkey_button.set_sensitive(not model.is_busy)
            self.passkey_button.set_busy(model.is_busy, "Waiting…")
            self.passkey_cancel_button.set_label("Cancel" if model.is_busy else "Back")
            self.passkey_waiting_label.set_visible(stage.is_passkey and model.is_busy)

            if model.error_message:
                self.error_label.set_markup(
                    f'<span foreground="#e01b24">{GLib.markup_escape_text(model.error_message)}</span>')
            self.error_label.set_visible(bool(model.error_message))
        finally:
            self._rendering = False

    @staticmethod
    def _set_entry_text(entry: Gtk.Entry, text: str) -> None:
        if entry.get_text() != text:
            entry.set_text(text)

    @property
    def visible_page(self) -> Optional[str]:
        return self.stack.get_visible_child_name()

    @property
    def error_text(self) -> Optional[str]:
        return self.error_label.get_text() if self.error_label.get_visible() else None

    # MARK: Entry → model

    def _on_email_changed(self, entry: Gtk.Entry) -> None:
        if not self._rendering:
            self._model.email = entry.get_text()
            self.render()

    def _on_password_changed(self, entry: Gtk.Entry) -> None:
        if not self._rendering:
            self._model.password = entry.get_text()
            self.render()

    def _on_code_changed(self, entry: Gtk.Entry) -> None:
        if not self._rendering:
            self._model.code = entry.get_text()
            self.render()

    # MARK: Buttons → model

    def _on_sign_in(self, _button: Gtk.Button) -> None:
        if self._model.can_submit_credentials:
            self._model.sign_in_with_password()

    def _on_email_code(self, _button: Gtk.Button) -> None:
        if self._model.can_submit_credentials:
            self._model.send_email_code()

    def _on_continue(self, _button: Gtk.Button) -> None:
        if self._model.code and not self._model.is_busy:
            self._model.submit_code()

    def _on_restart(self, _button: Gtk.Button) -> None:
        self._model.restart()

    def _on_passkey(self, _button: Gtk.Button) -> None:
        url = self._model.passkey_verification_url
        if url is not None:
            try:
                Gtk.show_uri_on_window(self, url, Gdk.CURRENT_TIME)
            except GLib.Error as error:
                # Keep waiting anyway: the page may already be open, and the waiting
                # text tells the user to click again if it isn't.
                log.app.warning("Couldn't open the passkey page in a browser: %s", error.message)
        self._model.wait_for_passkey()


# MARK: Controller

class LoginWindowController:
    """Owns the one login window and its model. ``app`` (duck-typed, may be None) gets
    ``present_after_login()`` once a session is established — the equivalent of the macOS
    ``NSApp.activate`` that pulls focus back after the browser passkey flow."""

    def __init__(self, vault: EnteVault, api: EnteAPI, app: Optional[object] = None,
                 login: Optional[EnteLogin] = None, dispatch: Dispatch = _dispatch) -> None:
        self._vault = vault
        self._api = api
        self._app = app
        self._login = login
        self._dispatch = dispatch
        self.model: Optional[LoginViewModel] = None
        self.window: Optional[LoginWindow] = None

    def show(self) -> None:
        if self.window is None:
            self.window = self._build()
        self.window.show()
        self.window.present()
        if self.window.visible_page == PAGE_CREDENTIALS and self.model is not None:
            # After a failed attempt the email is still filled in; put the caret where
            # the user will type next.
            focus = self.window.password_entry if self.model.trimmed_email else self.window.email_entry
            focus.grab_focus()

    def close(self) -> None:
        if self.window is not None:
            self.window.hide()

    def _build(self) -> LoginWindow:
        if install_css is not None:
            install_css()
        model = LoginViewModel(self._vault, self._api, self._dispatch, login=self._login)
        model.on_signed_in = self._on_signed_in
        self.model = model
        window = LoginWindow(model)
        app = self._app
        if app is not None and hasattr(app, "add_window"):
            app.add_window(window)
        window.connect("delete-event", self._on_delete)
        return window

    def _on_signed_in(self) -> None:
        self.close()
        app = self._app
        if app is not None and hasattr(app, "present_after_login"):
            app.present_after_login()

    def _on_delete(self, window: Gtk.Window, _event: Gdk.Event) -> bool:
        window.hide()
        return True  # keep the window (and its model) for the next show()

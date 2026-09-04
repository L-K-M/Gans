"""Delivers a one-time code into whatever field had focus before Quick Search appeared —
the port of ``CodeInjector.swift``.

The flow: Quick Search records the focused window, then on commit it hides its own
window, re-activates that one, and synthesizes key events into it. Where macOS needs
the Accessibility permission, Linux needs an X server with XTest (native X11, or
XWayland); when that's missing the code is left on the clipboard and the caller is told
so it can explain.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable, Optional

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from .. import log  # noqa: E402
from ..prefs import DeliveryMode  # noqa: E402
from .clipboard import Clipboard  # noqa: E402
from .x11 import X11Session  # noqa: E402

__all__ = ["CodeInjector", "DeliveryResult"]


class DeliveryResult(Enum):
    DELIVERED = "delivered"
    #: No way to inject keystrokes; the code is on the clipboard instead.
    COPIED_ONLY = "copiedOnly"


class CodeInjector:
    """Types or pastes codes into other windows; falls back to copying."""

    #: Give the target a beat to come to the front before posting events.
    ACTIVATION_DELAY_MS = 120

    def __init__(self, clipboard: Clipboard, x11: X11Session):
        self.clipboard = clipboard
        self._x11 = x11

    @property
    def can_inject(self) -> bool:
        """Whether keystrokes can be synthesized at all (an X server with XTest)."""
        return self._x11.available and self._x11.has_xtest

    # MARK: Delivery

    def deliver(self, code: str, target_window: Optional[int], mode: DeliveryMode, also_copy: bool,
                clear_clipboard_after: Optional[float] = None,
                completion: Optional[Callable[[DeliveryResult], None]] = None) -> DeliveryResult:
        """Re-activates ``target_window`` and delivers ``code`` per ``mode``. Returns
        whether it *expects* to inject keystrokes or could only copy; ``completion``
        reports the actual outcome once the deferred keystroke work has run (an X error
        mid-way degrades to ``COPIED_ONLY`` with the code on the clipboard). When the code
        lands on the clipboard, ``clear_clipboard_after`` (seconds) schedules its wipe."""
        can_inject = self.can_inject
        copied = False
        if mode is DeliveryMode.PASTE or also_copy or not can_inject:
            copied = self.clipboard.copy(code, clear_after=clear_clipboard_after)

        if not can_inject:
            if completion is not None:
                completion(DeliveryResult.COPIED_ONLY)
            return DeliveryResult.COPIED_ONLY

        if target_window is not None:
            self._x11.activate_window(target_window)

        GLib.timeout_add(self.ACTIVATION_DELAY_MS, self._inject, code, mode, copied, clear_clipboard_after,
                         completion)
        return DeliveryResult.DELIVERED

    def _inject(self, code: str, mode: DeliveryMode, copied: bool, clear_after: Optional[float],
                completion: Optional[Callable[[DeliveryResult], None]]) -> bool:
        """The deferred keystroke work, on the main loop. Pasting needs the code on the
        clipboard; if that failed there's nothing to paste, so type instead."""
        if mode is DeliveryMode.PASTE and copied:
            delivered = self._x11.send_ctrl_v()
        else:
            delivered = self._x11.type_text(code)

        result = DeliveryResult.DELIVERED
        if not delivered:
            log.paste.warning("Couldn't inject the code; leaving it on the clipboard instead")
            if not copied:
                self.clipboard.copy(code, clear_after=clear_after)
            result = DeliveryResult.COPIED_ONLY
        if completion is not None:
            completion(result)
        return False  # one-shot GLib source

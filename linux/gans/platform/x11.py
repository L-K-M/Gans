"""X11 access for what Wayland deliberately withholds from ordinary clients: which window
has focus, handing focus back to it, synthesizing keystrokes into other apps, and grabbing
a global hotkey. This is the Linux stand-in for ``NSWorkspace.frontmostApplication`` /
``NSRunningApplication.activate``, ``CGEventPost`` (``CodeInjector.swift``) and the Carbon
hotkey (``HotkeyManager.swift``). Under a Wayland session it talks to XWayland, which
Mutter and KWin bridge to the real focus and input.

Everything goes through python-xlib. A ``Display`` object is **not** thread-safe, so
``X11Session`` serialises every request behind a lock and ``X11HotkeyGrabber`` runs its
event loop on a connection of its own. Constructing an ``X11Session`` never fails: without
a usable ``$DISPLAY`` it reports ``available == False`` and every method degrades to
``None`` / ``False`` so callers fall back to the clipboard instead of crashing.
"""

from __future__ import annotations

import os
import select
import threading
import time
from typing import Callable, List, NamedTuple, Optional, Sequence, Tuple

from Xlib import X, XK
from Xlib import display as xdisplay
from Xlib import error as xerror
from Xlib.ext import xtest
from Xlib.protocol import event as xevent

from .. import log
from ..hotkeyspec import HotkeySpec

__all__ = ["X11Session", "X11HotkeyGrabber"]

#: Pause between synthesized key events — xdotool's default. XTest input is ordered by
#: construction; the pause keeps apps that debounce or auto-repeat from merging events.
_KEY_DELAY = 0.012
#: After changing the keyboard mapping, before pressing the remapped key: lets the target
#: see MappingNotify first.
_REMAP_DELAY = 0.03
#: After pressing a remapped key, before restoring the mapping: GTK/Qt translate a key
#: event lazily against the *current* server map, so the symbol must stay bound until
#: the target has processed the press (xdotool has the same race and the same cure).
_REMAP_SETTLE = 0.05
#: Don't hammer a dead ``$DISPLAY``: wait this long before trying to connect again.
_CONNECT_RETRY = 5.0
#: ``ISO_Level3_Shift`` (AltGr) — missing from python-xlib's keysym tables.
_LEVEL3_KEYSYM = 0xFE03
#: The eight modifier bits of an event's ``state`` (buttons live above them).
_MODIFIER_MASK = 0xFF
#: Non-Latin-1 characters are typed through their Unicode keysym (``0x01000000 + code``).
_UNICODE_KEYSYM_BASE = 0x01000000

XK.load_keysym_group("xf86")  # media keys, for hotkeys like XF86AudioPlay


class _Untypeable(Exception):
    """Raised while planning a keystroke sequence when a character has no key and no
    keycode is free to be remapped."""


class _Step(NamedTuple):
    """One character of a typing plan: either an existing key plus the modifier keys to
    hold, or (``keycode is None``) a keysym to bind to the scratch keycode first."""
    keycode: Optional[int]
    modifiers: Tuple[int, ...]
    keysym: int


def _keysym_for_char(character: str) -> int:
    """Latin-1 characters *are* their keysyms (so ``string_to_keysym`` isn't needed);
    everything else uses the Unicode keysym form, which XKB and every toolkit accept.
    A layout may spell the same character with a legacy keysym (``EuroSign`` rather than
    ``U20AC``); such characters just take the scratch-keycode route."""
    code = ord(character)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return _UNICODE_KEYSYM_BASE + code


def _humanize_class(name: str) -> str:
    """``firefox`` → ``Firefox``; GTK4 apps use their app id (``org.gnome.Nautilus``)."""
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name[:1].upper() + name[1:]


# MARK: - Keyboard mapping

class _Keymap:
    """A fresh snapshot of the server's core keyboard mapping — read at the start of every
    typing request so a layout switched since we connected is honoured.

    The core map is what XKB exports to non-XKB clients: for each keycode a row of keysyms
    by *position*, laid out ``[G1L1, G1L2, G2L1, G2L2, G1L3, G1L4, G2L3, G2L4]`` (group,
    level). The first two levels of every group come first, then levels 3–4 (the AltGr
    symbols). A single-group layout has group 2 duplicated from group 1. Level 2 needs
    Shift, level 3 ``ISO_Level3_Shift`` (AltGr), level 4 both.
    """

    _POSITIONS = ((1, 0), (1, 1), (2, 0), (2, 1), (1, 2), (1, 3), (2, 2), (2, 3))

    def __init__(self, display: xdisplay.Display):
        info = display.display.info
        self.first = info.min_keycode
        count = info.max_keycode - info.min_keycode + 1
        self.rows: List[Sequence[int]] = list(display.get_keyboard_mapping(self.first, count))

    def key_for(self, keysym: int) -> Optional[Tuple[int, int]]:
        """``(keycode, level)`` for the lowest level at which a key produces ``keysym``
        in **every** group.

        Without the XKB extension (python-xlib has none) we can't ask which group —
        i.e. which of the user's layouts — is active, and the server applies the active
        group to synthesized keys too: pressing the ``a`` key while a ``us,ru`` user is on
        Russian types ``ф``. So a key only qualifies when a group switch couldn't change
        what it types (digits, most punctuation, every key of a single-layout setup).
        Anything else goes through the scratch keycode, which is bound with a single
        group and therefore types the same symbol whatever group is locked."""
        best: Optional[Tuple[int, int]] = None
        for offset, row in enumerate(self.rows):
            for position, symbol in enumerate(row):
                if symbol != keysym or position >= len(self._POSITIONS):
                    continue
                group, level = self._POSITIONS[position]
                if group != 1:
                    continue
                twin = self._POSITIONS.index((2, level))
                if twin < len(row) and row[twin] != keysym:
                    continue
                if best is None or level < best[1]:
                    best = (self.first + offset, level)
        return best

    def any_key_for(self, keysym: int) -> int:
        """The first keycode carrying ``keysym`` at any position, or 0 — for modifier and
        lock keys, whose keysym is the same in every group."""
        for offset, row in enumerate(self.rows):
            if keysym in row:
                return self.first + offset
        return 0

    def free_keycode(self) -> Optional[int]:
        """A keycode bound to nothing, usable as scratch space — xdotool's trick."""
        for offset, row in enumerate(self.rows):
            if all(symbol == X.NoSymbol for symbol in row):
                return self.first + offset
        return None


# MARK: - X11Session

class X11Session:
    """A lazily opened connection to ``$DISPLAY`` for focus queries, focus hand-off and
    XTest typing. Safe to construct anywhere; safe to call from any thread."""

    def __init__(self, display_name: Optional[str] = None):
        self._display_name = display_name if display_name is not None else os.environ.get("DISPLAY")
        self._lock = threading.RLock()
        self._display: Optional[xdisplay.Display] = None
        self._has_xtest: Optional[bool] = None
        self._retry_at = 0.0

    # MARK: Connection

    @property
    def display_name(self) -> Optional[str]:
        return self._display_name or None

    @property
    def available(self) -> bool:
        """Whether an X server is reachable (connects on first use)."""
        with self._lock:
            return self._connect() is not None

    @property
    def has_xtest(self) -> bool:
        """Whether the server offers the XTEST extension — required for typing."""
        with self._lock:
            display = self._connect()
            if display is None:
                return False
            if self._has_xtest is None:
                self._has_xtest = display.query_extension("XTEST") is not None
            return self._has_xtest

    def _connect(self) -> Optional[xdisplay.Display]:
        """The open display, connecting if needed. Failures are remembered briefly so a
        missing server doesn't cost a connection attempt per call."""
        if self._display is not None:
            return self._display
        if not self._display_name or time.monotonic() < self._retry_at:
            return None
        try:
            display = xdisplay.Display(self._display_name)
        except (xerror.DisplayError, OSError, ValueError, OverflowError) as error:
            # DisplayError covers bad names and refused connections; the socket layer
            # adds OSError, and malformed display numbers surface as Value/OverflowError.
            log.paste.info("No X display at %s: %s", self._display_name, error)
            self._retry_at = time.monotonic() + _CONNECT_RETRY
            return None
        display.set_error_handler(self._on_protocol_error)
        self._display = display
        self._has_xtest = None
        return display

    def _on_protocol_error(self, error, request) -> None:
        """Asynchronous X errors (from requests without replies that weren't wrapped in a
        ``CatchError``) — worth a debug line, never a crash."""
        log.paste.debug("X protocol error: %s", error)

    def _drop_connection(self) -> None:
        """After an I/O failure the connection is unusable; forget it so the next call
        reconnects (or reports unavailability)."""
        display, self._display = self._display, None
        self._has_xtest = None
        if display is not None:
            try:
                display.close()
            except (xerror.ConnectionClosedError, OSError):
                pass

    def close(self) -> None:
        with self._lock:
            self._drop_connection()

    # MARK: Windows

    def active_window(self) -> Optional[int]:
        """The focused toplevel's window id — the app a code should be delivered to.

        ``_NET_ACTIVE_WINDOW`` on the root is what an EWMH window manager maintains; the
        fallback (no WM, or one that doesn't publish it) walks up from the X input focus
        to the client window that carries ``WM_CLASS``, like ``xdotool getwindowfocus``."""
        with self._lock:
            display = self._connect()
            if display is None:
                return None
            try:
                root = display.screen().root
                active = root.get_full_property(display.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
                if active is not None and active.value and active.value[0]:
                    return int(active.value[0])
                window = display.get_input_focus().focus
                if isinstance(window, int) or window.id == root.id:  # None / PointerRoot / root
                    return None
                for _ in range(32):
                    if window.get_wm_class():
                        return int(window.id)
                    parent = window.query_tree().parent
                    if isinstance(parent, int) or parent.id == root.id:
                        return int(window.id)
                    window = parent
                return int(window.id)
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                self._handle_failure("active_window", error)
                return None

    def activate_window(self, window_id: int) -> None:
        """Gives ``window_id`` the focus back (the ``previousApp.activate`` of the macOS
        app). Two mechanisms, both harmless if the window is gone:

        * a ``_NET_ACTIVE_WINDOW`` client message with source indication 2 (pager) and a
          zero timestamp — Mutter and KWin honour pager requests without the
          focus-stealing check that would otherwise leave the target merely flashing;
        * ``XSetInputFocus`` for the WM-less case and window managers that ignore the
          message."""
        with self._lock:
            display = self._connect()
            if display is None or not window_id:
                return
            try:
                root = display.screen().root
                window = display.create_resource_object("window", window_id)
                message = xevent.ClientMessage(window=window,
                                               client_type=display.intern_atom("_NET_ACTIVE_WINDOW"),
                                               data=(32, [2, 0, 0, 0, 0]))
                errors = xerror.CatchError()
                root.send_event(message, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
                                onerror=errors)
                window.set_input_focus(X.RevertToParent, X.CurrentTime, onerror=errors)
                display.sync()
                if errors.get_error() is not None:
                    log.paste.debug("Couldn't activate window %#x: %s", window_id, errors.get_error())
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                self._handle_failure("activate_window", error)

    def window_name(self, window_id: int) -> Optional[str]:
        """A human name for the window's app, for "Insert into Firefox": the ``WM_CLASS``
        class name (stable per app), else the window title."""
        with self._lock:
            display = self._connect()
            if display is None or not window_id:
                return None
            try:
                window = display.create_resource_object("window", window_id)
                wm_class = window.get_wm_class()
                if wm_class and any(char.isalnum() for char in wm_class[1]):
                    return _humanize_class(wm_class[1])
                for name in ("_NET_WM_NAME", "WM_NAME"):
                    title = window.get_full_property(display.intern_atom(name), X.AnyPropertyType)
                    if title is None or not title.value:
                        continue
                    value = title.value
                    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                    if text.strip():
                        return text.strip()
                return None
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                self._handle_failure("window_name", error)
                return None

    # MARK: Typing

    def type_text(self, text: str) -> bool:
        """Types ``text`` into whatever has focus, layout-independently: every character is
        resolved against the live keyboard mapping (with the Shift / AltGr key held as
        needed) and characters the layout can't produce are bound to an unused keycode
        for the duration of one keystroke. Returns False — having typed nothing — when a
        character can't be produced or the server is unreachable."""
        with self._lock:
            display = self._connect()
            if display is None or not self.has_xtest:
                return False
            try:
                keymap = _Keymap(display)
                steps = [self._plan(keymap, character) for character in text]
                self._execute(display, keymap, steps)
                return True
            except _Untypeable as error:
                log.paste.warning("Can't type the code with this keyboard: %s", error)
                return False
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                self._handle_failure("type_text", error)
                return False

    def send_ctrl_v(self) -> bool:
        """Synthesizes Ctrl+V (the ⌘V of ``CodeInjector.sendCommandV``)."""
        with self._lock:
            display = self._connect()
            if display is None or not self.has_xtest:
                return False
            try:
                keymap = _Keymap(display)
                control = keymap.any_key_for(XK.XK_Control_L) or keymap.any_key_for(XK.XK_Control_R)
                v_key = keymap.any_key_for(XK.XK_v)
                if not control or not v_key:
                    log.paste.warning("This keyboard mapping has no Control or V key; can't paste")
                    return False
                self._key(display, X.KeyPress, control)
                self._tap(display, v_key)
                self._key(display, X.KeyRelease, control)
                return True
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                self._handle_failure("send_ctrl_v", error)
                return False

    def _plan(self, keymap: _Keymap, character: str) -> _Step:
        keysym = _keysym_for_char(character)
        found = keymap.key_for(keysym)
        if found is not None:
            keycode, level = found
            modifiers = self._modifier_keys(keymap, level)
            if modifiers is not None:
                return _Step(keycode, modifiers, keysym)
        return _Step(None, (), keysym)

    @staticmethod
    def _modifier_keys(keymap: _Keymap, level: int) -> Optional[Tuple[int, ...]]:
        """The modifier keycodes to hold for ``level`` (bit 0: Shift, bit 1: AltGr), or
        None when the layout has no key for a needed modifier."""
        keys: List[int] = []
        if level & 1:
            shift = keymap.any_key_for(XK.XK_Shift_L) or keymap.any_key_for(XK.XK_Shift_R)
            if not shift:
                return None
            keys.append(shift)
        if level & 2:
            level3 = keymap.any_key_for(_LEVEL3_KEYSYM)
            if not level3:
                return None
            keys.append(level3)
        return tuple(keys)

    def _execute(self, display: xdisplay.Display, keymap: _Keymap, steps: Sequence[_Step]) -> None:
        scratch = None
        if any(step.keycode is None for step in steps):
            scratch = keymap.free_keycode()
            if scratch is None:
                raise _Untypeable("no unused keycode to remap for a symbol the layout lacks")

        # Caps Lock inverts every letter we'd type (Steam codes!) and even the scratch
        # key, so toggle it off for the duration and restore it afterwards.
        caps_lock = 0
        if display.screen().root.query_pointer().mask & X.LockMask:
            caps_lock = keymap.any_key_for(XK.XK_Caps_Lock)
        if caps_lock:
            self._tap(display, caps_lock)
        try:
            for step in steps:
                if step.keycode is None:
                    self._type_via_scratch(display, scratch, step.keysym)
                    continue
                for modifier in step.modifiers:
                    self._key(display, X.KeyPress, modifier)
                self._tap(display, step.keycode)
                for modifier in reversed(step.modifiers):
                    self._key(display, X.KeyRelease, modifier)
        finally:
            if caps_lock:
                self._tap(display, caps_lock)

    def _type_via_scratch(self, display: xdisplay.Display, scratch: int, keysym: int) -> None:
        """Binds ``keysym`` to the unused ``scratch`` keycode, presses it, and unbinds it
        again — exactly what xdotool does for symbols the layout lacks."""
        display.change_keyboard_mapping(scratch, [[keysym]])
        display.sync()
        time.sleep(_REMAP_DELAY)
        try:
            self._tap(display, scratch)
            display.sync()
            time.sleep(_REMAP_SETTLE)
        finally:
            display.change_keyboard_mapping(scratch, [[X.NoSymbol]])
            display.sync()

    @staticmethod
    def _key(display: xdisplay.Display, event_type: int, keycode: int) -> None:
        xtest.fake_input(display, event_type, keycode)
        display.sync()
        time.sleep(_KEY_DELAY)

    def _tap(self, display: xdisplay.Display, keycode: int) -> None:
        self._key(display, X.KeyPress, keycode)
        self._key(display, X.KeyRelease, keycode)

    def _handle_failure(self, operation: str, error: Exception) -> None:
        if isinstance(error, xerror.BadWindow):
            # The target closed between Quick Search opening and the commit — routine.
            log.paste.debug("X11 %s: the window is gone", operation)
            return
        log.paste.warning("X11 %s failed: %s", operation, error)
        if isinstance(error, (xerror.ConnectionClosedError, OSError)):
            self._drop_connection()


# MARK: - X11HotkeyGrabber

def _keysym_for_spec(spec: HotkeySpec) -> int:
    """GDK key names and X keysym names agree except for case quirks (``space`` vs
    ``Space``) and python-xlib spelling media keys ``XF86_AudioPlay``."""
    name = spec.x11_keysym_name
    candidates = [name, name.lower(), name.capitalize()]
    if name.startswith("XF86") and not name.startswith("XF86_"):
        candidates.append("XF86_" + name[4:])
    for candidate in candidates:
        keysym = XK.string_to_keysym(candidate)
        if keysym:
            return keysym
    return 0


def _spec_modifiers(spec: HotkeySpec) -> int:
    mask = 0
    if spec.control:
        mask |= X.ControlMask
    if spec.alt:
        mask |= X.Mod1Mask
    if spec.shift:
        mask |= X.ShiftMask
    if spec.super_:
        mask |= X.Mod4Mask
    return mask


def _lock_masks(display: xdisplay.Display) -> List[int]:
    """The modifier bits of Num Lock, Caps Lock and Scroll Lock as currently mapped
    (Num Lock is Mod2 almost everywhere, but the modifier map is the truth). A grab has
    to be repeated for every combination of these so the hotkey works with them on."""
    masks: List[int] = []
    modifier_map = display.get_modifier_mapping()
    for keysym in (XK.XK_Num_Lock, XK.XK_Caps_Lock, XK.XK_Scroll_Lock):
        keycode = display.keysym_to_keycode(keysym)
        if not keycode:
            continue
        for index, keycodes in enumerate(modifier_map):
            if keycode in keycodes:
                mask = 1 << index
                if mask not in masks:
                    masks.append(mask)
                break
    return masks


class X11HotkeyGrabber:
    """A global hotkey through ``XGrabKey`` on the root window — the backend for classic
    X11 desktops (and the last resort elsewhere; under Wayland it only sees keys while an
    X11 window is focused).

    The grab and its events belong to one X connection, so ``register`` opens a private
    connection, grabs on it, and hands it to a daemon thread that selects on the
    connection plus a wake-up pipe; ``unregister`` pokes the pipe, joins the thread,
    ungrabs and closes. Matching presses are handed to ``dispatch(on_pressed)`` so the
    callback runs on the main loop."""

    def __init__(self, x11: X11Session, dispatch: Callable[[Callable[[], None]], object]):
        self._x11 = x11
        self._dispatch = dispatch
        self._lock = threading.RLock()
        self._display: Optional[xdisplay.Display] = None
        self._thread: Optional[threading.Thread] = None
        self._wake: Optional[Tuple[int, int]] = None
        self._grabs: List[Tuple[int, int]] = []

    def register(self, spec: HotkeySpec, on_pressed: Callable[[], None]) -> bool:
        """Grabs ``spec``; replaces any previous grab. False when the key is unknown to
        this server or another client already owns the combination (``BadAccess``)."""
        with self._lock:
            self.unregister()
            if not self._x11.available:
                return False
            try:
                display = xdisplay.Display(self._x11.display_name)
            except (xerror.DisplayError, OSError, ValueError, OverflowError) as error:
                log.hotkey.warning("Couldn't open a display for the hotkey grab: %s", error)
                return False
            try:
                match = self._grab(display, spec)
            except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                log.hotkey.warning("X11 hotkey grab failed: %s", error)
                match = None
            if match is None:
                self._grabs = []
                display.close()
                return False
            self._display = display
            self._wake = os.pipe()
            self._thread = threading.Thread(target=self._run, name="gans-x11-hotkey", daemon=True,
                                            args=(display, self._wake[0], spec, match, on_pressed))
            self._thread.start()
            log.hotkey.info("X11 grab registered for %s", spec.display_string)
            return True

    def _grab(self, display: xdisplay.Display, spec: HotkeySpec) -> Optional[Tuple[int, int, int]]:
        """Grabs every lock-key variant of ``spec``. Returns what the listener must match
        — ``(keycode, modifiers, ignored_lock_bits)`` — or None when the grab failed."""
        keysym = _keysym_for_spec(spec)
        keycode = display.keysym_to_keycode(keysym) if keysym else 0
        if not keycode:
            log.hotkey.warning("No key for %r on this keyboard; can't grab %s", spec.key, spec.display_string)
            return None
        modifiers = _spec_modifiers(spec)
        locks = _lock_masks(display)
        combinations = set()
        for bits in range(1 << len(locks)):
            combination = modifiers
            for index, mask in enumerate(locks):
                if bits & (1 << index):
                    combination |= mask
            combinations.add(combination)

        root = display.screen().root
        errors = xerror.CatchError()
        self._grabs = []
        for combination in sorted(combinations):
            root.grab_key(keycode, combination, False, X.GrabModeAsync, X.GrabModeAsync, onerror=errors)
            self._grabs.append((keycode, combination))
        display.sync()
        failure = errors.get_error()
        if failure is not None:
            self._ungrab(display)
            if isinstance(failure, xerror.BadAccess):
                log.hotkey.warning("%s is already grabbed by another client", spec.display_string)
            else:
                log.hotkey.warning("Grabbing %s failed: %s", spec.display_string, failure)
            return None
        ignored = 0
        for mask in locks:
            ignored |= mask
        return keycode, modifiers, ignored

    def _ungrab(self, display: xdisplay.Display) -> None:
        root = display.screen().root
        errors = xerror.CatchError()
        for keycode, modifiers in self._grabs:
            root.ungrab_key(keycode, modifiers, onerror=errors)
        self._grabs = []
        display.sync()

    def _run(self, display: xdisplay.Display, wake_fd: int, spec: HotkeySpec,
             match: Tuple[int, int, int], on_pressed: Callable[[], None]) -> None:
        keycode, modifiers, ignored = match
        display_fd = display.fileno()
        try:
            while True:
                readable, _, _ = select.select([display_fd, wake_fd], [], [])
                if wake_fd in readable:
                    return
                while display.pending_events():
                    event = display.next_event()
                    if (event.type == X.KeyPress and event.detail == keycode
                            and (event.state & _MODIFIER_MASK & ~ignored) == modifiers):
                        self._dispatch(on_pressed)
        except (xerror.XError, xerror.ConnectionClosedError, OSError, ValueError) as error:
            log.hotkey.warning("X11 hotkey listener for %s stopped: %s", spec.display_string, error)

    def unregister(self) -> None:
        """Releases the grab and stops the listener thread (no-op when not registered)."""
        with self._lock:
            thread, self._thread = self._thread, None
            wake, self._wake = self._wake, None
            display, self._display = self._display, None
            if wake is not None:
                os.write(wake[1], b"x")
            if thread is not None:
                thread.join(timeout=5)
            if wake is not None:
                os.close(wake[0])
                os.close(wake[1])
            if display is not None:
                try:
                    self._ungrab(display)
                    display.close()
                except (xerror.XError, xerror.ConnectionClosedError, OSError) as error:
                    log.hotkey.debug("Releasing the X11 grab failed: %s", error)
                self._grabs = []

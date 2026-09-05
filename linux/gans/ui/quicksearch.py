"""The Spotlight-style Quick Search: a floating, undecorated window with a large search
field over a results list — the port of ``QuickSearchView.swift`` and
``QuickSearchPanel.swift``.

``QuickSearchWindow`` renders the model; ``QuickSearchController`` owns the experience:
it shows the window, remembers the window that had focus, routes navigation keys, and on
commit delivers the selected code into that window through the ``CodeInjector``.

macOS keys map to Linux as: ⌘1–9 → Ctrl+1–9, ⌘C → Ctrl+C, ⌥↩ → Alt+Return, holding ⌥
(peek) → holding Alt, holding ⌘ (quick-pick badges) → holding Ctrl.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
import cairo  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11  # noqa: E402
except (ImportError, ValueError):  # a GTK built without the X11 backend
    GdkX11 = None

from .. import log  # noqa: E402
from ..entry import AuthEntry  # noqa: E402
from ..platform.inject import DeliveryResult  # noqa: E402
from ..prefs import Preferences  # noqa: E402
from .css import ACCENT_RGB, contrasting_foreground, install_css  # noqa: E402
from .issuerchip import IssuerChip  # noqa: E402
from .quicksearch_model import QuickSearchModel  # noqa: E402

__all__ = ["QuickSearchController", "QuickSearchWindow", "Metrics"]

Anchor = Tuple[int, int]  # (left, top) in screen pixels


# MARK: Metrics

class Metrics:
    """Fixed layout metrics shared by the content and the controller, so the window
    height is computed deterministically (no size-negotiation round-trips) and the window
    can be anchored by its top edge while the list grows and shrinks."""

    WIDTH = 560
    SEARCH_FIELD_HEIGHT = 60
    DIVIDER_HEIGHT = 1
    ROW_HEIGHT = 44
    ROW_SPACING = 2
    LIST_PADDING = 8
    MAX_LIST_HEIGHT = 320
    EMPTY_STATE_HEIGHT = 64
    FOOTER_HEIGHT = 26
    RING_SIZE = 16
    CHIP_SIZE = 26

    @classmethod
    def list_height(cls, rows: int) -> int:
        """The results list hugs its content up to ``MAX_LIST_HEIGHT`` — a greedy list
        would leave dead space under short result sets."""
        if rows <= 0:
            return 0
        content = rows * cls.ROW_HEIGHT + (rows - 1) * cls.ROW_SPACING + cls.LIST_PADDING * 2
        return min(content, cls.MAX_LIST_HEIGHT)

    @classmethod
    def panel_height(cls, rows: int) -> int:
        if rows <= 0:
            return cls.SEARCH_FIELD_HEIGHT + cls.DIVIDER_HEIGHT + cls.EMPTY_STATE_HEIGHT
        return cls.SEARCH_FIELD_HEIGHT + cls.DIVIDER_HEIGHT + cls.list_height(rows) + cls.FOOTER_HEIGHT


#: Ring colors as the code nears expiry (system orange / red).
_RING_WARNING = (1.0, 0.58, 0.0)
_RING_CRITICAL = (1.0, 0.23, 0.19)
_ON_ACCENT = contrasting_foreground(ACCENT_RGB)

#: Spurious focus-out events happen while the window is being mapped; ignore them for
#: this long after ``show()``.
_FOCUS_OUT_GRACE = 0.3
#: Code/ring refresh cadence while the window is up (the ring sweeps at 4 fps).
_TICK_INTERVAL_MS = 250

#: Keyval → result index for Ctrl+1…9, on the main row and the keypad.
_DIGIT_KEYS: Dict[int, int] = {
    **{key: index for index, key in enumerate(range(Gdk.KEY_1, Gdk.KEY_9 + 1))},
    **{key: index for index, key in enumerate(range(Gdk.KEY_KP_1, Gdk.KEY_KP_9 + 1))},
}
_RETURN_KEYS = (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
_ALT_KEYS = (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R)
_CONTROL_KEYS = (Gdk.KEY_Control_L, Gdk.KEY_Control_R)


# MARK: Countdown ring

class _CountdownRing(Gtk.DrawingArea):
    """A small circular countdown that depletes over the code's period and warms to
    amber/red as expiry nears, so you can tell at a glance whether to wait for the next
    code. Drawn on every tick; ``precise_fraction_remaining`` keeps the sweep smooth."""

    def __init__(self) -> None:
        super().__init__()
        self._fraction = 1.0
        self._seconds = 0
        self._selected = False
        self.set_size_request(Metrics.RING_SIZE, Metrics.RING_SIZE)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._on_draw)

    def update(self, fraction: float, seconds: int, selected: bool) -> None:
        if (fraction, seconds, selected) == (self._fraction, self._seconds, self._selected):
            return
        self._fraction = fraction
        self._seconds = seconds
        self._selected = selected
        self.set_tooltip_text(f"{seconds} seconds remaining")
        self.queue_draw()

    def _on_draw(self, _widget: Gtk.Widget, context: cairo.Context) -> bool:
        allocation = self.get_allocation()
        size = float(Metrics.RING_SIZE)
        centre_x = allocation.width / 2.0
        centre_y = allocation.height / 2.0
        radius = size / 2.0 - 1.0
        tint = _ON_ACCENT if self._selected else ACCENT_RGB
        if self._seconds <= 5:
            color = _RING_CRITICAL
        elif self._seconds <= 10:
            color = _RING_WARNING
        else:
            color = tint

        context.set_line_width(2.0)
        context.set_source_rgba(*tint, 0.25)
        context.arc(centre_x, centre_y, radius, 0, 2 * math.pi)
        context.stroke()

        fraction = max(0.001, min(self._fraction, 1.0))
        start = -math.pi / 2
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_source_rgb(*color)
        context.arc(centre_x, centre_y, radius, start, start + 2 * math.pi * fraction)
        context.stroke()
        return True


# MARK: Result row

class _ResultRow(Gtk.ListBoxRow):
    """A single result row: chip (or quick-pick badge), name and account on the left,
    masked/live code and countdown ring on the right."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_entry: Optional[AuthEntry] = None
        self.set_activatable(True)
        self.set_can_focus(False)
        self.set_size_request(-1, Metrics.ROW_HEIGHT)
        self.get_style_context().add_class("gans-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.add(box)

        leading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        leading.set_valign(Gtk.Align.CENTER)
        self._chip = IssuerChip("", Metrics.CHIP_SIZE)
        self._badge = Gtk.Label()
        self._badge.set_size_request(Metrics.CHIP_SIZE, Metrics.CHIP_SIZE)
        self._badge.get_style_context().add_class("gans-quick-pick")
        leading.pack_start(self._chip, False, False, 0)
        leading.pack_start(self._badge, False, False, 0)
        box.pack_start(leading, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.CENTER)
        text.set_hexpand(True)
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self._title = Gtk.Label(xalign=0.0)
        self._title.set_ellipsize(Pango.EllipsizeMode.END)
        self._title.get_style_context().add_class("gans-issuer")
        self._pin = Gtk.Image.new_from_icon_name("view-pin-symbolic", Gtk.IconSize.MENU)
        self._pin.set_pixel_size(10)
        self._pin.get_style_context().add_class("gans-pin")
        title.pack_start(self._title, False, True, 0)
        title.pack_start(self._pin, False, False, 0)
        self._account = Gtk.Label(xalign=0.0)
        self._account.set_ellipsize(Pango.EllipsizeMode.END)
        self._account.get_style_context().add_class("gans-account")
        text.pack_start(title, False, False, 0)
        text.pack_start(self._account, False, False, 0)
        box.pack_start(text, True, True, 0)

        self._code = Gtk.Label()
        self._code.get_style_context().add_class("gans-code")
        self._ring = _CountdownRing()
        box.pack_start(self._code, False, False, 0)
        box.pack_start(self._ring, False, False, 0)
        self.show_all()

    def update(self, entry: AuthEntry, index: int, model: QuickSearchModel) -> None:
        """Refreshes every label from ``entry`` and the model's display state; cheap
        enough to run on every tick (GTK skips unchanged label text)."""
        self.auth_entry = entry
        name = entry.issuer or entry.display_name
        self._chip.set_name(name)
        self._title.set_text(name)
        self._pin.set_visible(entry.pinned)
        has_account = bool(entry.account) and bool(entry.issuer)
        self._account.set_text(entry.account if has_account else "")
        self._account.set_visible(has_account)

        # The Ctrl+N quick-pick badge replaces the chip while Ctrl is held (first 9 rows).
        show_badge = model.show_indices and index < 9
        self._badge.set_text(f"Ctrl+{index + 1}" if show_badge else "")
        self._badge.set_visible(show_badge)
        self._chip.set_visible(not show_badge)

        # A dot mask sized to the code, so a hidden row still reads as "a code lives here".
        if model.codes_visible:
            self._code.set_text(entry.formatted_code(model.tick))
        else:
            self._code.set_text("•" * max(entry.digits, 4))

        selected = entry.id == model.selected_id
        style = self.get_style_context()
        if selected:
            style.add_class("gans-row-selected")
        else:
            style.remove_class("gans-row-selected")

        self._ring.set_visible(entry.is_time_based)
        if entry.is_time_based:
            self._ring.update(entry.precise_fraction_remaining(model.tick), entry.seconds_remaining(model.tick),
                              selected)

        help_text = entry.display_name if not entry.note else f"{entry.display_name} — {entry.note}"
        self.set_tooltip_text(help_text)

    # MARK: Read-only state (for tests and accessibility tooling)

    @property
    def is_selected(self) -> bool:
        return self.get_style_context().has_class("gans-row-selected")

    @property
    def code_text(self) -> str:
        return self._code.get_text()

    @property
    def badge_text(self) -> Optional[str]:
        """The quick-pick badge text while it replaces the chip, else None."""
        return self._badge.get_text() if self._badge.get_visible() else None

    @property
    def chip(self) -> IssuerChip:
        return self._chip


# MARK: Window

class QuickSearchWindow(Gtk.Window):
    """The floating panel: undecorated, kept above, off the taskbar, fixed width. It
    renders ``model`` and calls ``on_commit`` when a row is clicked; keyboard handling is
    the controller's (connected to this window's key events)."""

    def __init__(self, model: QuickSearchModel, on_commit: Callable[[AuthEntry], None]):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        install_css()
        self._model = model
        self._on_commit = on_commit
        #: A pool of row widgets: the first ``len(_row_ids)`` are bound to the current
        #: results, the rest are hidden spares (rows are recycled, never rebuilt).
        self._rows: List[_ResultRow] = []
        self._row_ids: List[str] = []
        self._pending_scroll: Optional[float] = None
        #: The selection last scrolled into view; the list only scrolls when this changes.
        self._scrolled_to: Optional[str] = None
        #: A model change arrived while hidden; ``render()`` runs on the next show.
        self._stale = False
        self._applied_geometry: Optional[Tuple[int, Optional[Anchor]]] = None
        #: Screen position of the top-left corner, fixed while the window is up — the
        #: window is pinned by its TOP edge and horizontal centre so the search field
        #: never jumps while the result list below it grows and shrinks. None on
        #: Wayland, where the compositor places the window.
        self.anchor: Optional[Anchor] = None

        self.set_title("Gans Quick Search")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_default_size(Metrics.WIDTH, Metrics.panel_height(0))
        style = self.get_style_context()
        style.add_class("gans-quick-search")
        # Rounded corners need an alpha channel *and* a compositor to blend it; without
        # one, transparent corners render black, so fall back to square corners.
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
        else:
            style.add_class("gans-square")

        self._build()
        self._model.on_change(self._on_model_changed)
        self.connect("show", self._on_show)

    # MARK: Construction

    def _build(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.set_size_request(Metrics.WIDTH, -1)
        self.add(root)

        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        search_row.set_size_request(-1, Metrics.SEARCH_FIELD_HEIGHT)
        search_row.set_margin_start(18)
        search_row.set_margin_end(18)
        icon = Gtk.Image.new_from_icon_name("edit-find-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        icon.set_pixel_size(20)
        icon.set_valign(Gtk.Align.CENTER)
        icon.get_style_context().add_class("gans-search-icon")
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Search Ente Auth…")
        self.entry.set_has_frame(False)
        self.entry.set_hexpand(True)
        self.entry.set_valign(Gtk.Align.CENTER)
        self.entry.get_style_context().add_class("gans-search-entry")
        self.entry.connect("changed", self._on_entry_changed)
        search_row.pack_start(icon, False, False, 0)
        search_row.pack_start(self.entry, True, True, 0)
        root.pack_start(search_row, False, False, 0)

        root.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        self._empty = Gtk.Label()
        self._empty.set_size_request(-1, Metrics.EMPTY_STATE_HEIGHT)
        self._empty.set_ellipsize(Pango.EllipsizeMode.END)
        self._empty.get_style_context().add_class("gans-empty")
        root.pack_start(self._empty, False, False, 0)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_can_focus(False)
        self._scroller.set_propagate_natural_height(False)
        self._scroller.get_vadjustment().connect("changed", self._on_adjustment_changed)
        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.set_activate_on_single_click(True)
        self._list.set_can_focus(False)
        self._list.set_margin_top(Metrics.LIST_PADDING)
        self._list.set_margin_bottom(Metrics.LIST_PADDING)
        self._list.set_margin_start(Metrics.LIST_PADDING)
        self._list.set_margin_end(Metrics.LIST_PADDING)
        self._list.connect("row-activated", self._on_row_activated)
        self._scroller.add(self._list)
        root.pack_start(self._scroller, False, False, 0)

        self._footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self._footer.set_size_request(-1, Metrics.FOOTER_HEIGHT)
        self._footer.get_style_context().add_class("gans-footer")
        self._insert_hint, self._insert_label = self._make_hint("↩", "Insert")
        self._copy_hint, _ = self._make_hint("Alt+↩", "Copy")
        self._peek_hint, _ = self._make_hint("Alt", "Peek")
        self._quick_pick_hint, _ = self._make_hint("Ctrl+1–9", "Quick pick")
        self._dismiss_hint, _ = self._make_hint("esc", "Dismiss")
        for hint in (self._insert_hint, self._copy_hint, self._peek_hint, self._quick_pick_hint):
            self._footer.pack_start(hint, False, False, 0)
        self._footer.pack_end(self._dismiss_hint, False, False, 0)
        root.pack_start(self._footer, False, False, 0)

        # Show everything once; from here on visibility is managed piecewise by render()
        # (the window itself is shown with show()/present(), never show_all()).
        root.show_all()
        self.render()

    @staticmethod
    def _make_hint(key: str, label: str) -> Tuple[Gtk.Box, Gtk.Label]:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)
        key_label = Gtk.Label(label=key)
        key_label.get_style_context().add_class("gans-hint-key")
        text_label = Gtk.Label(label=label)
        text_label.get_style_context().add_class("gans-hint")
        box.pack_start(key_label, False, False, 0)
        box.pack_start(text_label, False, False, 0)
        return box, text_label

    # MARK: Read-only state (for tests and accessibility tooling)

    @property
    def rows(self) -> Sequence[_ResultRow]:
        """The rows bound to the current results, in order (hidden spares excluded)."""
        return tuple(self._rows[:len(self._row_ids)])

    @property
    def empty_text(self) -> Optional[str]:
        """The empty-state message while it's showing, else None."""
        return self._empty.get_text() if self._empty.get_visible() else None

    @property
    def insert_hint_text(self) -> str:
        return self._insert_label.get_text()

    @property
    def peek_hint_visible(self) -> bool:
        return self._peek_hint.get_visible()

    @property
    def list_adjustment(self) -> Gtk.Adjustment:
        return self._scroller.get_vadjustment()

    # MARK: Rendering

    def _on_model_changed(self) -> None:
        """Model notifications re-render at once while the window is up. While it's
        hidden they're coalesced: ``show()`` sets several model properties in a row, and
        one render before mapping is enough."""
        if self.get_visible():
            self.render()
        else:
            self._stale = True

    def _on_show(self, _window: Gtk.Widget) -> None:
        if self._stale:
            self.render()

    def render(self) -> None:
        """Re-renders from the model. Row widgets are pooled: when the set/order of
        result ids changes the pool is re-bound in place (grown only when the list gets
        longer than ever before, surplus rows hidden), otherwise labels and the selection
        are updated in place — so typing never churns widgets and the tick is cheap."""
        self._stale = False
        model = self._model
        results = model.results
        ids = [entry.id for entry in results]
        if ids != self._row_ids:
            self._bind_rows(len(results))
            self._row_ids = ids
            self._scrolled_to = None  # the rows under the selection moved: re-centre it
        for index, (row, entry) in enumerate(zip(self._rows, results)):
            row.update(entry, index, model)

        if results:
            self._empty.set_visible(False)
            self._scroller.set_visible(True)
            self._footer.set_visible(True)
            self._update_footer()
            self._scroll_to_selected()
        else:
            self._scroller.set_visible(False)
            self._footer.set_visible(False)
            if model.query:
                self._empty.set_text(f"No matches for “{model.query}”")
            elif model.has_entries:
                self._empty.set_text("Type to search your codes")
            else:
                self._empty.set_text("No codes yet — add them in the Ente Auth app")
            self._empty.set_visible(True)
        self.relayout()

    def _bind_rows(self, count: int) -> None:
        """Sizes the pool to ``count`` visible rows. Rows are created only when the list
        outgrows the pool and hidden (not destroyed) when it shrinks, so a keystroke that
        narrows or widens the results costs a rebind, not a rebuild of every row."""
        while len(self._rows) < count:
            row = _ResultRow()
            if self._rows:
                row.set_margin_top(Metrics.ROW_SPACING)
            self._list.add(row)
            self._rows.append(row)
        for index, row in enumerate(self._rows):
            if index < count:
                row.set_visible(True)
            elif row.get_visible():
                row.set_visible(False)
                row.auth_entry = None  # a hidden spare holds no entry
        self._scroller.set_size_request(-1, Metrics.list_height(count))

    def _update_footer(self) -> None:
        """A quiet key-hint bar, so Alt-peek, Alt+↩ and Ctrl+1–9 are discoverable without
        docs. When the query has narrowed to a single hit, name the app the code will be
        typed into — reinforcing the core trick ("↩ to fill into Firefox")."""
        model = self._model
        app_name = model.target_app_name
        if len(model.results) == 1 and app_name:
            self._insert_label.set_text(f"Fill into {app_name}")
        else:
            self._insert_label.set_text("Insert")
        # Peeking only means something while codes are masked (the default). When the
        # "Show codes" preference is on, the codes are already visible, so skip it.
        self._peek_hint.set_visible(not model.show_codes)

    def _scroll_to_selected(self) -> None:
        """Keeps the highlighted row in view (centred, like ``scrollTo(anchor: .center)``)
        — but only when the selection changes (or the rows under it were re-bound), as the
        macOS ``onChange(of: selectedID)`` does. Every other render, the 4 Hz tick above
        all, leaves the list where the user's wheel put it.

        The geometry is known from the metrics, so no allocation round-trip is needed;
        the value is re-applied once the adjustment learns its new range."""
        selected = self._model.selected_id
        if selected == self._scrolled_to:
            return
        self._scrolled_to = selected
        self._pending_scroll = None
        rows = len(self._row_ids)
        viewport = Metrics.list_height(rows)
        content = rows * Metrics.ROW_HEIGHT + max(rows - 1, 0) * Metrics.ROW_SPACING + Metrics.LIST_PADDING * 2
        if content <= viewport:
            return
        index = next((position for position, row_id in enumerate(self._row_ids) if row_id == selected), None)
        if index is None:
            return
        centre = Metrics.LIST_PADDING + index * (Metrics.ROW_HEIGHT + Metrics.ROW_SPACING) + Metrics.ROW_HEIGHT / 2
        target = max(0.0, min(centre - viewport / 2, content - viewport))
        adjustment = self._scroller.get_vadjustment()
        if adjustment.get_upper() - adjustment.get_page_size() >= target:
            adjustment.set_value(target)
        else:
            self._pending_scroll = target

    def _on_adjustment_changed(self, adjustment: Gtk.Adjustment) -> None:
        pending = self._pending_scroll
        if pending is not None and adjustment.get_upper() - adjustment.get_page_size() >= pending:
            adjustment.set_value(pending)
            self._pending_scroll = None

    # MARK: Geometry

    def relayout(self) -> None:
        """Applies the deterministic size for the current row count and re-pins the top
        edge. Growing downward with a fixed top-left is exactly what an X11 resize does,
        so the move only matters on first placement and to correct a WM that shifted us."""
        height = Metrics.panel_height(len(self._model.results))
        geometry = (height, self.anchor)
        if geometry == self._applied_geometry:
            return
        self._applied_geometry = geometry
        self.resize(Metrics.WIDTH, height)
        if self.anchor is not None:
            self.move(*self.anchor)

    def forget_geometry(self) -> None:
        """Forces the next ``relayout`` to re-apply size and position (called on show,
        since the WM may have moved a hidden window's remembered position) and the next
        render to scroll the selection back into view (the list may have been left
        scrolled elsewhere when the window was dismissed)."""
        self._applied_geometry = None
        self._scrolled_to = None

    # MARK: Signals

    def _on_entry_changed(self, entry: Gtk.Entry) -> None:
        self._model.query = entry.get_text()

    def _on_row_activated(self, _list: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        if isinstance(row, _ResultRow) and row.auth_entry is not None:
            self._on_commit(row.auth_entry)


def _own_popup_holds_focus() -> bool:
    """Whether a widget of this process holds a grab — a ``Gtk.Menu`` popped up from our
    window (``gtk_grab_add`` plus a GDK keyboard grab), which is where the focus went."""
    if Gtk.grab_get_current() is not None:
        return True
    display = Gdk.Display.get_default()
    seat = display.get_default_seat() if display is not None else None
    keyboard = seat.get_keyboard() if seat is not None else None
    return keyboard is not None and display.device_is_grabbed(keyboard)


# MARK: Controller

class QuickSearchController:
    """Owns the Quick Search experience: shows the floating window, remembers the window
    that had focus, routes navigation keys, and on commit delivers the selected code into
    that window. ``app`` provides ``toast.show(...)`` and ``show_settings()``."""

    def __init__(self, prefs: Preferences, injector, x11, app) -> None:
        self._prefs = prefs
        self._injector = injector
        self._x11 = x11
        self._app = app
        self.model = QuickSearchModel()
        self._window: Optional[QuickSearchWindow] = None
        self._target: Optional[int] = None
        self._tick_source: Optional[int] = None
        self._shown_at = 0.0

        #: Supplies the current decrypted entries when the window opens.
        self.entries_provider: Callable[[], Sequence[AuthEntry]] = lambda: []
        self.is_signed_in: Callable[[], bool] = lambda: False
        #: Asked to present the login window when the user isn't signed in.
        self.on_needs_login: Callable[[], None] = lambda: None
        #: Whether the app is locked, and how to ask for unlock instead of showing codes.
        self.is_locked: Callable[[], bool] = lambda: False
        self.on_locked: Callable[[], None] = lambda: None
        #: Fired when a code is committed (typed or copied) so the tray item can play the
        #: copy confirmation — the glyph blink and, in honk mode, the honk.
        self.on_committed: Callable[[], None] = lambda: None

    @property
    def window(self) -> Optional[QuickSearchWindow]:
        """The window, once it has been created (lazily, on the first ``show()``)."""
        return self._window

    @property
    def is_visible(self) -> bool:
        return self._window is not None and self._window.get_visible()

    def toggle(self) -> None:
        if self.is_visible:
            self.hide()
        else:
            self.show()

    # MARK: Show / hide

    def show(self) -> None:
        if self.is_locked():
            self.on_locked()
            return
        if not self.is_signed_in():
            self.on_needs_login()
            return
        # Capture the window that currently has focus BEFORE we take it ourselves.
        self._target = self._x11.active_window()
        self.model.target_app_name = self._x11.window_name(self._target) if self._target is not None else None

        self.model.show_codes = self._prefs.show_codes_in_quick_search
        self.model.peek = False
        self.model.recent_ids = self._prefs.frecency_ranked_ids
        self.model.set_entries(list(self.entries_provider()))
        self.model.reset()

        window = self._ensure_window()
        window.entry.set_text("")
        window.anchor = self._anchor_on_pointer_monitor()
        window.forget_geometry()
        window.render()

        self._shown_at = time.monotonic()
        # Show first, then present: on a hidden window present() merely shows it and
        # leaves focusing to the WM (nobody, on a bare X server); on a visible one it
        # asks for focus outright with a fresh server timestamp.
        window.show()
        window.present()
        window.entry.grab_focus()
        self._start_ticking()

    def hide(self, restore_focus: bool = True) -> None:
        """Dismisses the window. ``restore_focus`` hands focus back to the window that
        was active before Quick Search opened — right for Esc/hotkey dismissal, wrong
        when the user clicked into another window (focus-out) or a commit is about to
        activate the target itself."""
        was_visible = self.is_visible
        if self._window is not None:
            self._window.hide()
        self._stop_ticking()
        self.model.show_indices = False
        self.model.peek = False
        if restore_focus and was_visible and self._target is not None:
            self._x11.activate_window(self._target)

    def _ensure_window(self) -> QuickSearchWindow:
        if self._window is None:
            window = QuickSearchWindow(self.model, self.commit)
            window.connect("key-press-event", self._on_key_press)
            window.connect("key-release-event", self._on_key_release)
            window.connect("focus-out-event", self._on_focus_out)
            window.connect("delete-event", self._on_delete)
            self._window = window
        return self._window

    # MARK: Placement

    def _anchor_on_pointer_monitor(self) -> Optional[Anchor]:
        """Top-left corner for the monitor under the pointer: horizontally centred, top
        edge a quarter of the way down the work area (the Spotlight position). Only on
        X11 — a Wayland compositor decides placement itself."""
        display = Gdk.Display.get_default()
        if display is None or GdkX11 is None or not isinstance(display, GdkX11.X11Display):
            return None
        monitor = None
        seat = display.get_default_seat()
        pointer = seat.get_pointer() if seat is not None else None
        if pointer is not None:
            _screen, pointer_x, pointer_y = pointer.get_position()
            monitor = display.get_monitor_at_point(pointer_x, pointer_y)
        if monitor is None:
            monitor = display.get_primary_monitor() or (display.get_monitor(0) if display.get_n_monitors() else None)
        if monitor is None:
            return None
        area = monitor.get_workarea()
        left = area.x + (area.width - Metrics.WIDTH) // 2
        top = area.y + int(area.height * 0.25)
        return left, top

    # MARK: Keyboard

    def _on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        keyval = event.keyval
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()

        # Holding Ctrl reveals the Ctrl+1…9 quick-pick badges; holding Alt peeks at the
        # masked codes (releasing re-masks) — fast, no settings round-trip.
        if keyval in _ALT_KEYS:
            self.model.peek = True
            return False
        if keyval in _CONTROL_KEYS:
            self.model.show_indices = True
            return False

        # Ctrl+1…9 (exactly Ctrl, so Ctrl+Shift+1 etc. stay out of the way) commits the
        # Nth result.
        if modifiers == Gdk.ModifierType.CONTROL_MASK and keyval in _DIGIT_KEYS:
            index = _DIGIT_KEYS[keyval]
            if index < len(self.model.results):
                self.commit(self.model.results[index])
            return True

        # Ctrl+C copies the selected code instead of inserting it.
        if modifiers == Gdk.ModifierType.CONTROL_MASK and Gdk.keyval_to_lower(keyval) == Gdk.KEY_c:
            entry = self.model.selected_entry
            if entry is not None:
                self.copy_commit(entry)
                return True
            return False

        if keyval == Gdk.KEY_Down:
            self.model.move_selection(down=True)
            return True
        if keyval == Gdk.KEY_Up:
            self.model.move_selection(down=False)
            return True
        if keyval in _RETURN_KEYS:
            entry = self.model.selected_entry
            if entry is not None:
                # Alt+Return copies without touching the previous window's focus.
                if modifiers == Gdk.ModifierType.MOD1_MASK:
                    self.copy_commit(entry)
                else:
                    self.commit(entry)
            return True
        if keyval == Gdk.KEY_Escape:
            if self.model.query and self._window is not None:
                self._window.entry.set_text("")  # flows back into the model via "changed"
            else:
                self.hide()
            return True
        return False  # let the entry handle typing

    def _on_key_release(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval in _ALT_KEYS:
            self.model.peek = False
        elif event.keyval in _CONTROL_KEYS:
            self.model.show_indices = False
        return False

    def _on_focus_out(self, _window: Gtk.Window, _event: Gdk.EventFocus) -> bool:
        """Dismiss like Spotlight when focus leaves the window (click another window).
        No focus restore — the user just chose somewhere else to be.

        Not when the "focus loss" is one of our own popups: the entry's context menu
        (right-click → Cut/Copy/Paste) takes a keyboard grab, which the X server reports
        as FocusOut(NotifyGrab) and GDK relays as a focus-out of the toplevel. Focus comes
        straight back when the menu closes, so there's nothing to dismiss."""
        if not self.is_visible or time.monotonic() - self._shown_at < _FOCUS_OUT_GRACE:
            return False
        if _own_popup_holds_focus():
            return False
        self.hide(restore_focus=False)
        return False

    def _on_delete(self, _window: Gtk.Window, _event: Gdk.Event) -> bool:
        self.hide(restore_focus=False)
        return True  # keep the window around for the next show()

    # MARK: Live code refresh

    def _start_ticking(self) -> None:
        self._stop_ticking()
        self._tick_source = GLib.timeout_add(_TICK_INTERVAL_MS, self._on_tick)

    def _stop_ticking(self) -> None:
        if self._tick_source is not None:
            GLib.source_remove(self._tick_source)
            self._tick_source = None

    def _on_tick(self) -> bool:
        if not self.is_visible:
            self._tick_source = None
            return False
        self.model.tick = time.time()
        return True

    # MARK: Commit

    def commit(self, entry: AuthEntry) -> None:
        """Types/pastes ``entry``'s code into the window that had focus."""
        target = self._target
        self._prefs.record_usage(entry.id)
        self.on_committed()
        self.hide(restore_focus=False)  # the injector re-activates the target itself

        # Never type a code that dies mid-submit: if a time-based code has ≤2 s left,
        # briefly wait for the next window and deliver the fresh code instead.
        if entry.is_time_based:
            remaining = entry.seconds_remaining()
            if remaining <= 2:
                self._app.toast.show("Waiting for a fresh code…", duration=remaining + 0.6)
                GLib.timeout_add(int((remaining + 0.15) * 1000), self._deliver_fresh, entry, target)
                return
        self._deliver(entry.code(), target)

    def _deliver_fresh(self, entry: AuthEntry, target: Optional[int]) -> bool:
        self._deliver(entry.code(), target)  # recomputed in the new window
        return False  # one-shot GLib source

    def _deliver(self, code: str, target: Optional[int]) -> None:
        self._injector.deliver(code, target, self._prefs.delivery_mode, self._prefs.also_copy_when_typing,
                               clear_clipboard_after=self._prefs.clipboard_clear_delay,
                               completion=self._on_delivered)

    def _on_delivered(self, result: DeliveryResult) -> None:
        # Without an X server (XTest) the code silently lands on the clipboard instead of
        # being typed — say so, and point at Settings, which explains the options.
        if result is DeliveryResult.COPIED_ONLY:
            self._app.toast.show("Copied to the clipboard — typing into other apps isn't available in this session.",
                                 duration=6, action_title="Settings…", action=self._app.show_settings)

    def copy_commit(self, entry: AuthEntry) -> None:
        """Copies the code without injecting it (Alt+Return / Ctrl+C), returning focus to
        where the user was."""
        code = entry.code()
        self._prefs.record_usage(entry.id)
        self.on_committed()
        self.hide()
        self._injector.clipboard.copy(code, clear_after=self._prefs.clipboard_clear_delay)
        self._app.toast.show("Code copied")
        log.app.debug("Quick Search copied a code")

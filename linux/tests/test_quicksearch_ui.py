"""Quick Search on a real (Xvfb) display: the window maps and focuses its entry, typing
filters and selects the top hit, the keyboard routes exactly as on macOS (Ctrl for ⌘,
Alt for ⌥), commits deliver the live code through the injector, and the window keeps
its width and top edge while the result list grows and shrinks.

Keys are driven with ``xdotool key --window`` (XSendEvent to the toplevel), which GTK
accepts with the right modifier state; ``Gtk.test_widget_send_key`` reports success
under Xvfb but delivers nothing. The collaborators — injector, X11 session, toasts —
are fakes that record calls.
"""

import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.harness import gtk_available, pump, wait_until
from tests.gtkbind import gtk_session

from gans.entry import AuthEntry
from gans.platform.inject import DeliveryResult
from gans.prefs import DeliveryMode, Preferences

SECRET = "JBSWY3DPEHPK3PXP"
TARGET_WINDOW = 0x2A00007

#: A TOTP instant 25 s before its window ends, and one 1 s before.
COMFORTABLE_TIME = 1_700_000_000 - (1_700_000_000 % 30) + 5.0
EXPIRING_TIME = 1_700_000_000 - (1_700_000_000 % 30) + 29.0


def make_entries():
    uris = [
        ("gh", f"otpauth://totp/GitHub:alice@example.com?secret={SECRET}&issuer=GitHub"),
        ("gg", f"otpauth://totp/Google:alice@gmail.com?secret={SECRET}&issuer=Google"),
        ("aws", f"otpauth://totp/Amazon%20Web%20Services:root?secret={SECRET}&issuer=Amazon%20Web%20Services"
                "&codeDisplay=%7B%22note%22%3A%22prod%20account%22%7D"),
        ("bank", f"otpauth://hotp/Bank:alice?secret={SECRET}&issuer=Bank&counter=5"),
    ]
    entries = [AuthEntry.parse(uri, entry_id) for entry_id, uri in uris]
    assert all(entries)
    return entries


# MARK: Fakes

class FakeClipboard:
    def __init__(self):
        self.copies = []

    def copy(self, text, clear_after=None):
        self.copies.append((text, clear_after))
        return True


class FakeInjector:
    def __init__(self):
        self.clipboard = FakeClipboard()
        self.deliveries = []
        self.result = DeliveryResult.DELIVERED

    def deliver(self, code, target_window, mode, also_copy, clear_clipboard_after=None, completion=None):
        self.deliveries.append((code, target_window, mode, also_copy, clear_clipboard_after))
        if completion is not None:
            completion(self.result)
        return self.result


class FakeX11:
    """Inert by default (``available`` False, no active window), like ``X11Session``
    without a ``$DISPLAY``; ``active``/``name`` make it report a target."""

    def __init__(self, active=None, name=None):
        self.available = active is not None
        self.active = active
        self.name = name
        self.activated = []

    def active_window(self):
        return self.active

    def window_name(self, window_id):
        return self.name if window_id == self.active else None

    def activate_window(self, window_id):
        self.activated.append(window_id)


class FakeToast:
    def __init__(self):
        self.shows = []

    def show(self, message, duration=2.4, action_title=None, action=None):
        self.shows.append((message, duration, action_title, action))

    @property
    def messages(self):
        return [item[0] for item in self.shows]


class FakeApp:
    def __init__(self):
        self.toast = FakeToast()
        self.settings_shown = 0

    def show_settings(self):
        self.settings_shown += 1


@unittest.skipUnless(gtk_available() and shutil.which("xdotool"), "needs GTK and xdotool")
class QuickSearchUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()
        from gans.ui.quicksearch import Metrics, QuickSearchController
        cls.Metrics = Metrics
        cls.Controller = QuickSearchController

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefs = Preferences(Path(self.tmp.name) / "preferences.json")
        self.injector = FakeInjector()
        self.x11 = FakeX11()
        self.app = FakeApp()
        self.entries = make_entries()
        self.locked = False
        self.signed_in = True
        self.events = []
        self.controller = None
        self.build_controller(self.x11)
        self.addCleanup(self._dispose)

    def build_controller(self, x11):
        """(Re)creates the controller under test around ``x11`` and the shared fakes."""
        self._dispose()
        self.x11 = x11
        self.controller = self.Controller(self.prefs, self.injector, x11, self.app)
        self.controller.entries_provider = lambda: self.entries
        self.controller.is_signed_in = lambda: self.signed_in
        self.controller.on_needs_login = lambda: self.events.append("login")
        self.controller.is_locked = lambda: self.locked
        self.controller.on_locked = lambda: self.events.append("locked")
        self.controller.on_committed = lambda: self.events.append("committed")
        return self.controller

    def _dispose(self):
        if self.controller is None:
            return
        for menu in self.popup_menus():  # a context menu left up would grab the next test
            menu.popdown()
        self.controller.hide(restore_focus=False)
        if self.controller.window is not None:
            self.controller.window.destroy()
        pump(50)

    # MARK: Helpers

    @property
    def window(self):
        return self.controller.window

    @property
    def model(self):
        return self.controller.model

    def show(self):
        self.controller.show()
        self.assertTrue(wait_until(lambda: self.window is not None and self.window.get_mapped()), "window not mapped")
        pump(100)
        return self.window

    def xdotool(self, *args):
        xid = self.window.get_window().get_xid()
        command = ["xdotool", args[0], "--window", str(xid), *args[1:]]
        subprocess.run(command, check=True, timeout=10)
        pump(150)

    def key(self, *keys):
        self.xdotool("key", *keys)

    def click(self, x, y, button=1):
        """A real pointer click at window-relative (x, y): the pointer is moved relative
        to the window, then pressed via XTest (a ``click --window`` XSendEvent never
        reaches the list's own event window)."""
        self.xdotool("mousemove", str(x), str(y))
        subprocess.run(["xdotool", "click", str(button)], check=True, timeout=10)
        pump(150)

    @staticmethod
    def popup_menus():
        """The ``Gtk.Menu``s currently popped up (each lives in its own POPUP_MENU
        toplevel), e.g. the entry's right-click context menu."""
        from gi.repository import Gdk, Gtk
        menus = []
        for toplevel in Gtk.Window.list_toplevels():
            if toplevel.get_type_hint() == Gdk.WindowTypeHint.POPUP_MENU and toplevel.get_mapped():
                child = toplevel.get_child()
                if isinstance(child, Gtk.Menu):
                    menus.append(child)
        return menus

    def by_id(self, entry_id):
        return next(entry for entry in self.entries if entry.id == entry_id)

    def selected_rows(self):
        return [row.auth_entry.id for row in self.window.rows if row.is_selected]

    # MARK: Showing

    def test_show_maps_the_window_focuses_the_entry_and_positions_it(self):
        self.assertFalse(self.controller.is_visible)
        self.assertIsNone(self.window)
        window = self.show()
        self.assertTrue(self.controller.is_visible)
        self.assertTrue(window.entry.has_focus())
        self.assertEqual(window.get_size()[0], self.Metrics.WIDTH)
        self.assertEqual(window.get_size()[1], self.Metrics.panel_height(4))
        # Xvfb is 1280x800 with no WM: the work area is the screen, so the panel sits
        # centred with its top a quarter of the way down.
        self.assertEqual(window.anchor, ((1280 - self.Metrics.WIDTH) // 2, 200))
        self.assertTrue(wait_until(lambda: window.get_position() == window.anchor), window.get_position())
        self.assertEqual([entry.id for entry in self.model.results], ["aws", "bank", "gh", "gg"])
        self.assertEqual(self.model.selected_id, "aws")
        self.assertEqual(self.selected_rows(), ["aws"])
        self.assertFalse(self.model.codes_visible)
        self.assertEqual(window.rows[0].get_tooltip_text(), "Amazon Web Services (root) — prod account")
        self.assertEqual(window.rows[2].get_tooltip_text(), "GitHub (alice@example.com)")

    def test_show_when_locked_asks_for_unlock_instead(self):
        self.locked = True
        self.controller.show()
        pump(50)
        self.assertEqual(self.events, ["locked"])
        self.assertIsNone(self.window)
        self.assertFalse(self.controller.is_visible)

    def test_show_when_signed_out_opens_login_instead(self):
        self.signed_in = False
        self.controller.show()
        pump(50)
        self.assertEqual(self.events, ["login"])
        self.assertIsNone(self.window)

    def test_toggle(self):
        self.controller.toggle()
        self.assertTrue(wait_until(lambda: self.controller.is_visible))
        self.controller.toggle()
        self.assertFalse(self.controller.is_visible)
        self.assertFalse(self.window.get_visible())

    def test_target_window_is_captured_before_showing(self):
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        window = self.show()
        self.assertEqual(self.model.target_app_name, "Firefox")
        window.entry.set_text("github")
        pump(50)
        self.assertEqual(len(self.model.results), 1)
        self.assertEqual(window.insert_hint_text, "Fill into Firefox")
        window.entry.set_text("g")
        pump(50)
        self.assertEqual(window.insert_hint_text, "Insert")

    # MARK: Filtering and navigation

    def test_typing_filters_and_selects_the_first_result(self):
        window = self.show()
        window.entry.set_text("g")
        pump(50)
        self.assertEqual(self.model.query, "g")
        self.assertEqual([entry.id for entry in self.model.results], ["gh", "gg"])
        self.assertEqual(self.model.selected_id, "gh")
        self.assertEqual(self.selected_rows(), ["gh"])
        self.assertEqual(len(window.rows), 2)
        window.entry.set_text("zzz")
        pump(50)
        self.assertEqual(len(window.rows), 0)
        self.assertEqual(window.empty_text, "No matches for “zzz”")

    def test_typed_keys_reach_the_entry(self):
        window = self.show()
        self.key("g", "i", "t")
        self.assertEqual(window.entry.get_text(), "git")
        self.assertEqual([entry.id for entry in self.model.results], ["gh"])

    def test_arrow_keys_move_the_selection(self):
        self.show()
        self.key("Down")
        self.assertEqual(self.model.selected_id, "bank")
        self.assertEqual(self.selected_rows(), ["bank"])
        self.key("Down")
        self.assertEqual(self.model.selected_id, "gh")
        self.key("Up")
        self.assertEqual(self.model.selected_id, "bank")
        self.assertEqual(self.selected_rows(), ["bank"])
        self.key("Up", "Up", "Up")
        self.assertEqual(self.model.selected_id, "aws")  # clamped at the top

    def test_escape_clears_the_query_then_hides(self):
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        window = self.show()
        window.entry.set_text("git")
        pump(50)
        self.key("Escape")
        self.assertEqual(window.entry.get_text(), "")
        self.assertEqual(self.model.query, "")
        self.assertTrue(self.controller.is_visible)
        self.assertEqual(len(self.model.results), 4)
        self.key("Escape")
        self.assertFalse(self.controller.is_visible)
        self.assertEqual(self.x11.activated, [TARGET_WINDOW])  # focus handed back

    def test_alt_peeks_and_ctrl_reveals_quick_pick_badges(self):
        window = self.show()
        row = window.rows[0]
        self.assertEqual(row.code_text, "••••••")
        self.xdotool("keydown", "alt")
        self.assertTrue(self.model.peek)
        self.assertTrue(self.model.codes_visible)
        self.assertEqual(row.code_text, self.by_id("aws").formatted_code(self.model.tick))
        self.xdotool("keyup", "alt")
        self.assertFalse(self.model.peek)
        self.assertEqual(row.code_text, "••••••")

        self.assertIsNone(row.badge_text)
        self.assertTrue(row.chip.get_visible())
        self.xdotool("keydown", "ctrl")
        self.assertTrue(self.model.show_indices)
        self.assertEqual(row.badge_text, "Ctrl+1")
        self.assertFalse(row.chip.get_visible())
        self.xdotool("keyup", "ctrl")
        self.assertFalse(self.model.show_indices)
        self.assertIsNone(row.badge_text)
        self.assertTrue(row.chip.get_visible())

    def test_show_codes_preference_reveals_codes_and_drops_the_peek_hint(self):
        self.prefs.show_codes_in_quick_search = True
        window = self.show()
        self.assertTrue(self.model.codes_visible)
        self.assertEqual(window.rows[0].code_text, self.by_id("aws").formatted_code(self.model.tick))
        self.assertFalse(window.peek_hint_visible)

    # MARK: Commit

    def test_return_commits_the_selected_entry(self):
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME), \
                patch("gans.entry._now", return_value=COMFORTABLE_TIME):
            window = self.show()
            window.entry.set_text("git")
            pump(50)
            self.key("Return")
            expected = self.by_id("gh").code()
        self.assertEqual(self.injector.deliveries,
                         [(expected, TARGET_WINDOW, DeliveryMode.TYPE, True, 30.0)])
        self.assertFalse(self.controller.is_visible)
        self.assertEqual(self.prefs.recently_used_ids, ["gh"])
        self.assertEqual(self.events, ["committed"])
        self.assertEqual(self.app.toast.shows, [])
        self.assertEqual(self.x11.activated, [])  # the injector re-activates the target itself
        self.assertEqual(self.injector.clipboard.copies, [])

    def test_ctrl_digit_commits_the_nth_result(self):
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME):
            window = self.show()
            window.entry.set_text("g")
            pump(50)
            self.key("ctrl+2")
            expected = self.by_id("gg").code()
        self.assertEqual([item[0] for item in self.injector.deliveries], [expected])
        self.assertEqual(self.prefs.recently_used_ids, ["gg"])
        self.assertFalse(self.controller.is_visible)

    def test_ctrl_digit_beyond_the_results_does_nothing(self):
        window = self.show()
        window.entry.set_text("g")
        pump(50)
        self.key("ctrl+5")
        self.assertEqual(self.injector.deliveries, [])
        self.assertTrue(self.controller.is_visible)

    def test_alt_return_copies_instead_of_typing(self):
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME):
            self.show()
            self.key("Down")
            self.key("alt+Return")
            expected = self.by_id("bank").code()
        self.assertEqual(self.injector.deliveries, [])
        self.assertEqual(self.injector.clipboard.copies, [(expected, 30.0)])
        self.assertEqual(self.app.toast.messages, ["Code copied"])
        self.assertEqual(self.prefs.recently_used_ids, ["bank"])
        self.assertEqual(self.events, ["committed"])
        self.assertFalse(self.controller.is_visible)
        self.assertEqual(self.x11.activated, [TARGET_WINDOW])  # copy hands focus back

    def test_ctrl_c_copies_the_selected_code(self):
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME):
            self.show()
            self.key("ctrl+c")
            expected = self.by_id("aws").code()
        self.assertEqual(self.injector.clipboard.copies, [(expected, 30.0)])
        self.assertEqual(self.injector.deliveries, [])

    def test_row_click_commits(self):
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME):
            window = self.show()
            metrics = self.Metrics
            row_centre_y = (metrics.SEARCH_FIELD_HEIGHT + metrics.DIVIDER_HEIGHT + metrics.LIST_PADDING
                            + (metrics.ROW_HEIGHT + metrics.ROW_SPACING) + metrics.ROW_HEIGHT // 2)
            self.click(metrics.WIDTH // 2, row_centre_y)
            expected = self.by_id("bank").code()
        self.assertEqual([item[0] for item in self.injector.deliveries], [expected])
        self.assertFalse(self.controller.is_visible)

    def test_commit_near_expiry_waits_for_a_fresh_code(self):
        """With ≤2 s left on a TOTP, commit waits for the next window and delivers the
        code computed *then* — not the one showing when Return was pressed. The clock is
        advanced during the wait so a stale, eagerly captured code would be caught."""
        clock = {"now": EXPIRING_TIME}
        with patch("gans.otp._now", side_effect=lambda: clock["now"]), \
                patch("gans.entry._now", side_effect=lambda: clock["now"]):
            self.show()
            aws = self.by_id("aws")
            self.assertEqual(aws.seconds_remaining(), 1)
            stale = aws.code()
            started = time.monotonic()
            self.key("Return")
            self.assertFalse(self.controller.is_visible)
            self.assertEqual(self.app.toast.shows, [("Waiting for a fresh code…", 1.6, None, None)])
            self.assertEqual(self.injector.deliveries, [])
            clock["now"] = EXPIRING_TIME + 2  # the next window arrives while we wait
            fresh = aws.code()
            self.assertNotEqual(fresh, stale)
            self.assertTrue(wait_until(lambda: self.injector.deliveries, timeout=4))
            self.assertGreaterEqual(time.monotonic() - started, 1.0)
        self.assertEqual([item[0] for item in self.injector.deliveries], [fresh])

    def test_hotp_commits_immediately(self):
        with patch("gans.otp._now", return_value=EXPIRING_TIME):
            self.show()
            self.key("Down")  # Bank (HOTP)
            self.key("Return")
        self.assertEqual([item[0] for item in self.injector.deliveries], [self.by_id("bank").code()])
        self.assertEqual(self.app.toast.shows, [])

    def test_copied_only_delivery_explains_and_offers_settings(self):
        self.injector.result = DeliveryResult.COPIED_ONLY
        with patch("gans.otp._now", return_value=COMFORTABLE_TIME):
            self.show()
            self.key("Return")
        self.assertEqual(len(self.app.toast.shows), 1)
        message, duration, action_title, action = self.app.toast.shows[0]
        self.assertIn("Copied to the clipboard", message)
        self.assertEqual(duration, 6)
        self.assertEqual(action_title, "Settings…")
        action()
        self.assertEqual(self.app.settings_shown, 1)

    # MARK: Geometry and dismissal

    def test_relayout_keeps_the_width_and_top_edge_while_results_change(self):
        window = self.show()
        metrics = self.Metrics
        self.assertEqual(window.get_size(), (metrics.WIDTH, metrics.panel_height(4)))
        window.entry.set_text("git")
        self.assertTrue(wait_until(lambda: window.get_size() == (metrics.WIDTH, metrics.panel_height(1))),
                        window.get_size())
        self.assertEqual(window.get_position(), window.anchor)
        window.entry.set_text("zzz")
        self.assertTrue(wait_until(lambda: window.get_size() == (metrics.WIDTH, metrics.panel_height(0))),
                        window.get_size())
        self.assertEqual(window.get_position(), window.anchor)
        window.entry.set_text("")
        self.assertTrue(wait_until(lambda: window.get_size() == (metrics.WIDTH, metrics.panel_height(4))),
                        window.get_size())
        self.assertEqual(window.get_position(), window.anchor)

    def test_long_lists_cap_the_height_and_scroll(self):
        self.entries = [AuthEntry.parse(f"otpauth://totp/Site{index:02d}:me?secret={SECRET}&issuer=Site{index:02d}",
                                        f"s{index}") for index in range(12)]
        window = self.show()
        self.assertEqual(window.get_size(), (self.Metrics.WIDTH, self.Metrics.panel_height(12)))
        self.assertEqual(self.Metrics.list_height(12), self.Metrics.MAX_LIST_HEIGHT)
        adjustment = window.list_adjustment
        self.assertEqual(adjustment.get_value(), 0.0)
        for _ in range(11):
            self.key("Down")
        self.assertEqual(self.model.selected_id, "s11")
        self.assertTrue(wait_until(lambda: adjustment.get_value() > 0), "the selected row wasn't scrolled into view")

    def test_wheel_scrolling_survives_the_tick(self):
        """The 4 Hz tick re-renders the rows; only a selection change may scroll the list
        (the macOS ``onChange(of: selectedID)``), so a list the user scrolled by hand
        stays put — otherwise rows past the first page could never be reached by mouse."""
        self.entries = [AuthEntry.parse(f"otpauth://totp/Site{index:02d}:me?secret={SECRET}&issuer=Site{index:02d}",
                                        f"s{index}") for index in range(12)]
        window = self.show()
        adjustment = window.list_adjustment
        self.assertTrue(wait_until(lambda: adjustment.get_upper() > adjustment.get_page_size()))
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        adjustment.set_value(bottom)  # what wheeling down to the last rows does
        first = self.model.tick
        self.assertTrue(wait_until(lambda: self.model.tick > first, timeout=2))
        pump(600)  # a couple more ticks
        self.assertEqual(adjustment.get_value(), bottom)
        self.assertEqual(self.model.selected_id, "s0")
        self.key("Down")  # a selection change still brings the highlight into view
        self.assertEqual(self.model.selected_id, "s1")
        self.assertTrue(wait_until(lambda: adjustment.get_value() == 0.0), adjustment.get_value())
        # Re-opening re-centres the selection rather than reviving the old scroll offset.
        adjustment.set_value(bottom)
        self.controller.hide()
        self.show()
        self.assertTrue(wait_until(lambda: adjustment.get_value() == 0.0), adjustment.get_value())

    def test_entry_context_menu_does_not_dismiss(self):
        """Right-clicking the search entry pops GTK's Cut/Copy/Paste menu, whose keyboard
        grab the X server reports as a focus-out of the toplevel. The panel must stay up
        (users right-click to paste a query), and closing the menu leaves it focused."""
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        window = self.show()
        pump(400)  # past the grace period for spurious focus-out events while mapping
        metrics = self.Metrics
        self.click(metrics.WIDTH // 2, metrics.SEARCH_FIELD_HEIGHT // 2, button=3)
        self.assertTrue(wait_until(lambda: self.popup_menus()), "no context menu popped up")
        pump(300)
        self.assertTrue(self.controller.is_visible)
        self.key("Escape")  # goes to the menu (it holds the grab), not to the panel
        self.assertTrue(wait_until(lambda: not self.popup_menus()), "the context menu stayed up")
        pump(300)
        self.assertTrue(self.controller.is_visible)
        self.assertTrue(wait_until(lambda: window.entry.has_focus()), "focus didn't return to the entry")
        self.assertEqual(self.x11.activated, [])

    def test_focus_out_hides_without_restoring_focus(self):
        from gi.repository import Gtk
        self.build_controller(FakeX11(active=TARGET_WINDOW, name="Firefox"))
        self.show()
        pump(400)  # past the grace period for spurious focus-out events while mapping
        other = Gtk.Window(title="Elsewhere")
        other.set_default_size(100, 100)
        other.show()
        other.present()
        try:
            self.assertTrue(wait_until(lambda: not self.controller.is_visible), "window stayed up after focus-out")
        finally:
            other.destroy()
        self.assertEqual(self.x11.activated, [])

    def test_tick_refreshes_codes_while_visible(self):
        self.prefs.show_codes_in_quick_search = True
        self.show()
        first = self.model.tick
        self.assertTrue(wait_until(lambda: self.model.tick > first, timeout=2))
        self.controller.hide()
        settled = self.model.tick
        pump(600)
        self.assertEqual(self.model.tick, settled)  # the timer stops with the window

    def test_live_entry_refresh_keeps_the_selection(self):
        window = self.show()
        self.key("Down", "Down")
        self.assertEqual(self.model.selected_id, "gh")
        refreshed = make_entries()[:3] + [AuthEntry.parse(f"otpauth://totp/Zed:me?secret={SECRET}&issuer=Zed", "zed")]
        self.model.set_entries(refreshed)
        pump(50)
        self.assertEqual(self.model.selected_id, "gh")
        self.assertEqual([row.auth_entry.id for row in window.rows], ["aws", "gh", "gg", "zed"])
        self.assertEqual(self.selected_rows(), ["gh"])

    def test_rows_are_recycled_when_the_results_change(self):
        """Narrowing and widening the results re-binds the existing row widgets (hiding
        the surplus) instead of destroying and rebuilding them all, so a keystroke with a
        large vault doesn't stall."""
        window = self.show()
        original = list(window.rows)
        self.assertEqual(len(original), 4)
        window.entry.set_text("g")
        pump(50)
        self.assertEqual(list(window.rows), original[:2])  # the same widgets, re-bound
        self.assertEqual([row.auth_entry.id for row in window.rows], ["gh", "gg"])
        self.assertEqual(self.selected_rows(), ["gh"])
        for spare in original[2:]:
            self.assertFalse(spare.get_visible())
            self.assertIsNone(spare.auth_entry)
        window.entry.set_text("")
        pump(50)
        self.assertEqual(list(window.rows), original)
        self.assertTrue(all(row.get_visible() for row in original))
        self.assertEqual([row.auth_entry.id for row in window.rows], ["aws", "bank", "gh", "gg"])
        self.assertEqual(self.selected_rows(), ["aws"])
        self.assertEqual(window.get_size(), (self.Metrics.WIDTH, self.Metrics.panel_height(4)))

    def test_reshowing_renders_once(self):
        """``show()`` sets several model properties in a row; while the window is hidden
        those notifications are coalesced into the single render made before mapping."""
        window = self.show()
        self.controller.hide()
        Row = type(window.rows[0])
        with patch.object(Row, "update", autospec=True, side_effect=Row.update) as update:
            self.show()
        self.assertEqual(update.call_count, 4)  # one render: each of the 4 rows bound once
        self.assertEqual([row.auth_entry.id for row in window.rows], ["aws", "bank", "gh", "gg"])


@unittest.skipUnless(gtk_available(), "needs GTK")
class StylesheetAndChipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = gtk_session()

    def test_stylesheet_parses_and_installs_once(self):
        from gans.ui.css import install_css
        self.assertTrue(install_css())
        self.assertTrue(install_css())

    def test_issuer_chip_draws_a_rounded_gradient_square(self):
        import cairo
        from gi.repository import Gtk
        from gans.ui.issuerchip import IssuerChip, chip_gradient

        host = Gtk.OffscreenWindow()
        chip = IssuerChip("GitHub", 26)
        host.add(chip)
        host.show_all()
        pump(100)
        try:
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 26, 26)
            chip.draw(cairo.Context(surface))
            surface.flush()
            data = surface.get_data()
            stride = surface.get_stride()

            def pixel(x, y):
                blue, green, red, alpha = data[y * stride + x * 4: y * stride + x * 4 + 4]
                return red, green, blue, alpha

            self.assertEqual(pixel(0, 0)[3], 0)   # rounded corner: nothing painted
            self.assertEqual(pixel(25, 25)[3], 0)
            top, _bottom = chip_gradient("GitHub")
            red, green, blue, alpha = pixel(13, 2)  # inside the fill, above the initials
            self.assertEqual(alpha, 255)
            for actual, expected in zip((red, green, blue), top):
                self.assertAlmostEqual(actual / 255.0, expected, delta=0.06)
            self.assertEqual(pixel(2, 13)[3], 255)
            self.assertNotEqual(pixel(13, 13)[:3], pixel(13, 2)[:3])  # the initials are drawn
            chip.set_name("Amazon Web Services")
            self.assertEqual(chip.name, "Amazon Web Services")
        finally:
            host.destroy()


if __name__ == "__main__":
    unittest.main()

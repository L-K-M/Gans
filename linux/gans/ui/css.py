"""The app stylesheet, installed once per screen at application priority.

Everything derives from the running GTK theme's colors (``@theme_bg_color``,
``@theme_fg_color``) so the windows look native in both light and dark themes; the one
hardcoded color is the app accent (``AccentColor.colorset`` on macOS: #1CB86B). The
foreground drawn over the accent — white or black — is chosen by actual WCAG contrast
ratio rather than a luminance threshold, because mid-tone accents (this green among
them) fool the threshold; see ``contrasting_foreground``.
"""

from __future__ import annotations

from typing import Dict, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .. import log  # noqa: E402

__all__ = ["ACCENT_RGB", "ACCENT_HEX", "contrasting_foreground", "css_color", "install_css",
           "relative_luminance", "stylesheet"]

RGB = Tuple[float, float, float]

#: The app accent — rgb(0.11, 0.72, 0.42) in the macOS asset catalog.
ACCENT_RGB: RGB = (0.11, 0.72, 0.42)
ACCENT_HEX = "#1CB86B"

WHITE: RGB = (1.0, 1.0, 1.0)
BLACK: RGB = (0.0, 0.0, 0.0)

# MARK: Color math


def relative_luminance(rgb: RGB) -> float:
    """WCAG 2.x relative luminance of an sRGB color with components in 0...1."""
    def linear(component: float) -> float:
        return component / 12.92 if component <= 0.03928 else ((component + 0.055) / 1.055) ** 2.4
    red, green, blue = rgb
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def contrasting_foreground(fill: RGB) -> RGB:
    """Black or white — whichever has the higher WCAG contrast ratio against ``fill``.
    Comparing the two actual ratios (rather than testing luminance against a cut-off)
    picks correctly for mid-tone fills, e.g. black on a lime/green accent."""
    luminance = relative_luminance(fill)
    contrast_with_white = 1.05 / (luminance + 0.05)
    contrast_with_black = (luminance + 0.05) / 0.05
    return WHITE if contrast_with_white >= contrast_with_black else BLACK


def css_color(rgb: RGB, alpha: float = 1.0) -> str:
    """``rgba(r, g, b, a)`` with 0...255 components, as GTK CSS wants it."""
    red, green, blue = (max(0, min(255, round(component * 255))) for component in rgb)
    return f"rgba({red}, {green}, {blue}, {alpha:g})"


# MARK: Stylesheet


def stylesheet() -> str:
    """The full GTK CSS. Class names are shared with the Settings/Login windows."""
    accent = css_color(ACCENT_RGB)
    on_accent = contrasting_foreground(ACCENT_RGB)
    on_accent_solid = css_color(on_accent)
    on_accent_dim = css_color(on_accent, 0.85)
    on_accent_faint = css_color(on_accent, 0.7)
    return f"""
/* Quick Search window: theme background, rounded corners (needs an RGBA visual + a
   compositor; .gans-square is added when either is missing) and a subtle edge. */
window.gans-quick-search {{
  background-color: @theme_bg_color;
  border-radius: 12px;
  border: 1px solid alpha(@theme_fg_color, 0.18);
}}
window.gans-quick-search.gans-square {{
  border-radius: 0;
}}
window.gans-quick-search list,
window.gans-quick-search viewport,
window.gans-quick-search scrolledwindow {{
  background-color: transparent;
  border: none;
}}

/* Search field: flat and large, no frame, the theme's caret. */
entry.gans-search-entry,
entry.gans-search-entry:focus {{
  background-color: transparent;
  background-image: none;
  border: none;
  box-shadow: none;
  outline-width: 0;
  padding: 0;
  min-height: 0;
  font-size: 22px;
  caret-color: @theme_fg_color;
}}
.gans-search-icon {{
  color: alpha(@theme_fg_color, 0.62);
}}

/* Result rows */
row.gans-row {{
  min-height: 44px;
  padding: 0 12px;
  border-radius: 8px;
  background-color: transparent;
  background-image: none;
  outline-width: 0;
}}
row.gans-row:hover,
row.gans-row.gans-row-hover {{
  background-color: alpha(@theme_fg_color, 0.06);
}}
row.gans-row.gans-row-selected,
row.gans-row.gans-row-selected:hover {{
  background-color: {accent};
  color: {on_accent_solid};
}}
row.gans-row.gans-row-selected label,
row.gans-row.gans-row-selected image {{
  color: {on_accent_solid};
}}
row.gans-row.gans-row-selected .gans-account,
row.gans-row.gans-row-selected .gans-quick-pick {{
  color: {on_accent_dim};
}}
row.gans-row.gans-row-selected .gans-pin {{
  color: {on_accent_faint};
}}
.gans-issuer {{
  font-size: 15px;
  font-weight: 500;
}}
.gans-account {{
  font-size: 12px;
  color: alpha(@theme_fg_color, 0.62);
}}
.gans-pin {{
  color: alpha(@theme_fg_color, 0.62);
}}
.gans-code {{
  font-family: monospace;
  font-size: 18px;
  font-weight: 600;
}}
.gans-quick-pick {{
  font-size: 12px;
  font-weight: 600;
  color: alpha(@theme_fg_color, 0.62);
}}

/* Empty state + key-hint footer */
.gans-empty {{
  font-size: 14px;
  color: alpha(@theme_fg_color, 0.62);
}}
.gans-footer {{
  min-height: 26px;
  padding: 0 14px;
}}
.gans-hint-key {{
  font-size: 10px;
  font-weight: 600;
  border-radius: 3px;
  padding: 1px 4px;
  background-color: alpha(@theme_fg_color, 0.08);
  color: alpha(@theme_fg_color, 0.62);
}}
.gans-hint {{
  font-size: 10px;
  color: alpha(@theme_fg_color, 0.62);
}}
"""


# MARK: Installation

_providers: Dict[Gdk.Screen, Gtk.CssProvider] = {}


def install_css() -> bool:
    """Adds the stylesheet to the default screen at ``APPLICATION`` priority. Idempotent
    per screen, so every window can call it defensively. Returns False (and logs) when
    there's no screen or the CSS fails to parse — the UI then simply looks unstyled."""
    screen = Gdk.Screen.get_default()
    if screen is None:
        log.app.warning("No default screen; the stylesheet can't be installed")
        return False
    if screen in _providers:
        return True
    provider = Gtk.CssProvider()
    try:
        provider.load_from_data(stylesheet().encode("utf-8"))
    except GLib.Error as error:
        log.app.error("The stylesheet failed to parse: %s", error.message)
        return False
    Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _providers[screen] = provider
    return True

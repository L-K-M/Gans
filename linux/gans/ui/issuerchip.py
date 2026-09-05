"""A small colored avatar showing an issuer's initial(s), tinted by a hue derived
deterministically from the issuer name — the port of ``IssuerChip.swift``. Makes rows
scannable at a glance without bundling any icon assets: GitHub is always the same
green-ish chip, AWS the same orange-ish one, run after run (and the same as on macOS,
since the hash is byte-for-byte the same FNV-1a fold over the same folded name).

``hue_for`` and ``initials_for`` are pure so they're unit-tested headless; only the
``IssuerChip`` widget needs GTK.
"""

from __future__ import annotations

import colorsys
import math
from typing import List, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

from .. import search  # noqa: E402

__all__ = ["IssuerChip", "chip_gradient", "hue_for", "initials_for"]

RGB = Tuple[float, float, float]

_FNV_OFFSET_BASIS = 1_469_598_103_934_665_603
_FNV_PRIME = 1_099_511_628_211
_MASK_64 = 0xFFFF_FFFF_FFFF_FFFF
#: Word separators for the initials (Swift: ``" -_.".contains``).
_SEPARATORS = " -_."

#: HSB saturation/brightness of the chip fill (``Color(hue:saturation:brightness:)``).
_SATURATION = 0.55
_BRIGHTNESS = 0.85
#: SwiftUI's ``.gradient`` darkens a color slightly towards the bottom edge.
_GRADIENT_DARKENING = 0.82

# MARK: Pure helpers


def hue_for(name: str) -> float:
    """A stable hue in ``[0, 1)`` from the name: 64-bit FNV-1a over the search-folded
    UTF-8 bytes, reduced to a degree. Empty (after folding) → 0."""
    folded = search.fold(name)
    if not folded:
        return 0.0
    digest = _FNV_OFFSET_BASIS
    for byte in folded.encode("utf-8"):
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _MASK_64
    return (digest % 360) / 360.0


def initials_for(name: str) -> str:
    """Up to two initials: first letters of the first two words, else the first two
    characters, uppercased. Falls back to "•" for an empty/symbol-only name."""
    words = [word for word in _split_words(name) if word]
    if len(words) >= 2:
        letters = words[0][0] + words[1][0]
    else:
        letters = name[:2]
    trimmed = letters.strip(" \t").upper()
    return trimmed or "•"


def _split_words(name: str) -> List[str]:
    words: List[str] = []
    current: List[str] = []
    for char in name:
        if char in _SEPARATORS:
            words.append("".join(current))
            current = []
        else:
            current.append(char)
    words.append("".join(current))
    return words


def chip_gradient(name: str) -> Tuple[RGB, RGB]:
    """The (top, bottom) sRGB fill colors for ``name``'s chip."""
    hue = hue_for(name)
    top = colorsys.hsv_to_rgb(hue, _SATURATION, _BRIGHTNESS)
    bottom = colorsys.hsv_to_rgb(hue, _SATURATION, _BRIGHTNESS * _GRADIENT_DARKENING)
    return top, bottom


# MARK: Widget


class IssuerChip(Gtk.DrawingArea):
    """A ``size``×``size`` rounded square with the issuer's initials, drawn with cairo."""

    def __init__(self, name: str, size: int = 26):
        super().__init__()
        self._name = name
        self._size = size
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._on_draw)

    @property
    def name(self) -> str:
        """The issuer this chip stands for."""
        return self._name

    def set_name(self, name: str) -> None:  # type: ignore[override]
        """Changes the issuer and redraws. This deliberately shadows ``Gtk.Widget.set_name``
        (the CSS id): a chip's only identity is its issuer, and rows are recycled by
        pointing the existing chip at a new name rather than rebuilding the widget."""
        if name == self._name:
            return
        self._name = name
        self.queue_draw()

    @property
    def size(self) -> int:
        return self._size

    def _on_draw(self, _widget: Gtk.Widget, context: cairo.Context) -> bool:
        allocation = self.get_allocation()
        size = float(self._size)
        # Centre the chip in whatever the parent allocated.
        origin_x = (allocation.width - size) / 2.0
        origin_y = (allocation.height - size) / 2.0
        top, bottom = chip_gradient(self._name)

        gradient = cairo.LinearGradient(origin_x, origin_y, origin_x, origin_y + size)
        gradient.add_color_stop_rgb(0.0, *top)
        gradient.add_color_stop_rgb(1.0, *bottom)
        _rounded_rectangle(context, origin_x, origin_y, size, size, size * 0.28)
        context.set_source(gradient)
        context.fill()

        layout = PangoCairo.create_layout(context)
        font = Pango.FontDescription("Sans")
        font.set_weight(Pango.Weight.BOLD)
        font.set_absolute_size(size * 0.42 * Pango.SCALE)
        layout.set_font_description(font)
        layout.set_text(initials_for(self._name), -1)
        _ink, logical = layout.get_pixel_extents()
        text_x = origin_x + (size - logical.width) / 2.0 - logical.x
        text_y = origin_y + (size - logical.height) / 2.0 - logical.y

        # A faint drop shadow keeps white initials legible on the lighter hues.
        context.set_source_rgba(0.0, 0.0, 0.0, 0.25)
        context.move_to(text_x, text_y + 0.5)
        PangoCairo.show_layout(context, layout)
        context.set_source_rgb(1.0, 1.0, 1.0)
        context.move_to(text_x, text_y)
        PangoCairo.show_layout(context, layout)
        return True


def _rounded_rectangle(context: cairo.Context, x: float, y: float, width: float, height: float,
                       radius: float) -> None:
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()

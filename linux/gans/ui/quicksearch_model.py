"""View model for the Quick Search panel: holds the query, the filtered results, and the
current selection — the port of ``QuickSearchModel.swift``. The window re-renders from
it whenever it changes; the controller feeds it the live entry list and handles commit.

Selection is tracked by **entry id**, not list offset: filtering reorders and shrinks
the results under stable row identities, so an offset-based selection produces stale
highlights (duplicate/none) and erratic arrow navigation. An id is stable across every
recompute.

Observers run synchronously on the caller's thread, which is always the GTK main
thread — the model is only ever touched from the main loop.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, List, Optional, Sequence

from .. import log, search
from ..entry import AuthEntry

__all__ = ["QuickSearchModel"]


class QuickSearchModel:
    def __init__(self) -> None:
        self._query = ""
        self._results: List[AuthEntry] = []
        self._selected_id: Optional[str] = None
        self._tick = time.time()
        self._show_codes = False
        self._peek = False
        self._show_indices = False
        self._target_app_name: Optional[str] = None
        self._all_entries: List[AuthEntry] = []
        self._recent_ids: List[str] = []
        self._observers: List[Callable[[], None]] = []

    # MARK: Query and results

    @property
    def query(self) -> str:
        return self._query

    @query.setter
    def query(self, value: str) -> None:
        self._query = value
        self._recompute(reset_selection=True)

    @property
    def results(self) -> List[AuthEntry]:
        """The filtered, ranked entries (read-only; recomputed on every change)."""
        return self._results

    @property
    def selected_id(self) -> Optional[str]:
        return self._selected_id

    @selected_id.setter
    def selected_id(self, value: Optional[str]) -> None:
        if value == self._selected_id:
            return
        self._selected_id = value
        self._notify()

    @property
    def selected_entry(self) -> Optional[AuthEntry]:
        if self._selected_id is None:
            return None
        return next((entry for entry in self._results if entry.id == self._selected_id), None)

    @property
    def has_entries(self) -> bool:
        """Whether the vault has any entries at all (drives the empty-state copy)."""
        return bool(self._all_entries)

    @property
    def recent_ids(self) -> List[str]:
        """Most-recently-used entry ids (most recent first), used to bias result ordering."""
        return list(self._recent_ids)

    @recent_ids.setter
    def recent_ids(self, value: Iterable[str]) -> None:
        self._recent_ids = list(value)
        self._recompute(reset_selection=False)

    def set_entries(self, entries: Sequence[AuthEntry]) -> None:
        self._all_entries = list(entries)
        self._recompute(reset_selection=False)

    def reset(self) -> None:
        self._query = ""
        self._recompute(reset_selection=True)

    # MARK: Display state

    @property
    def tick(self) -> float:
        """Epoch seconds of the last refresh; bumped so rows re-read their code and ring."""
        return self._tick

    @tick.setter
    def tick(self, value: float) -> None:
        self._tick = value
        self._notify()

    @property
    def show_codes(self) -> bool:
        """Whether rows reveal the live code (off by default — codes are masked and typed)."""
        return self._show_codes

    @show_codes.setter
    def show_codes(self, value: bool) -> None:
        self._set_flag("_show_codes", value)

    @property
    def peek(self) -> bool:
        """True while Alt is held — temporarily reveals masked codes."""
        return self._peek

    @peek.setter
    def peek(self, value: bool) -> None:
        self._set_flag("_peek", value)

    @property
    def show_indices(self) -> bool:
        """True while Ctrl is held — rows reveal their Ctrl+1…9 quick-pick badges."""
        return self._show_indices

    @show_indices.setter
    def show_indices(self, value: bool) -> None:
        self._set_flag("_show_indices", value)

    @property
    def codes_visible(self) -> bool:
        """Whether codes should be visible right now (the preference, or a live peek)."""
        return self._show_codes or self._peek

    @property
    def target_app_name(self) -> Optional[str]:
        """Name of the app that had focus when the panel opened — for the "↩ to fill into
        <App>" hint. None when unknown."""
        return self._target_app_name

    @target_app_name.setter
    def target_app_name(self, value: Optional[str]) -> None:
        if value == self._target_app_name:
            return
        self._target_app_name = value
        self._notify()

    # MARK: Navigation

    def move_selection(self, down: bool) -> None:
        """Move the highlight to the previous/next result, clamped at the ends. Anchors to
        the top if nothing valid is selected."""
        if not self._results:
            self.selected_id = None
            return
        current = self._index_of(self._selected_id)
        next_index = search.next_index(len(self._results), current, down)
        self.selected_id = self._results[next_index if next_index is not None else 0].id

    def _index_of(self, entry_id: Optional[str]) -> Optional[int]:
        if entry_id is None:
            return None
        return next((index for index, entry in enumerate(self._results) if entry.id == entry_id), None)

    # MARK: Observers

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        for callback in list(self._observers):
            try:
                callback()
            except Exception:
                log.app.exception("Quick Search observer failed")

    def _set_flag(self, attribute: str, value: bool) -> None:
        value = bool(value)
        if getattr(self, attribute) == value:
            return
        setattr(self, attribute, value)
        self._notify()

    def _recompute(self, reset_selection: bool) -> None:
        """Recomputes the filtered results. On a query change we re-anchor to the top match
        (so the best result is highlighted and Enter picks it); on a background refresh we
        keep the current selection if that entry is still present."""
        self._results = search.filter(self._all_entries, self._query, self._recent_ids)
        still_selectable = self._index_of(self._selected_id) is not None
        if reset_selection or not still_selectable:
            self._selected_id = self._results[0].id if self._results else None
        self._notify()

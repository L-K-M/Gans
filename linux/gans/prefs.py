"""App-wide settings, persisted as JSON under ``$XDG_CONFIG_HOME/gans/`` and observable by
the UI. Mirrors ``Preferences.swift`` (``UserDefaults`` there)."""

from __future__ import annotations

import json
import os
import threading
import time as _time
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .hotkeyspec import HotkeySpec
from . import log

__all__ = ["DeliveryMode", "Preferences", "config_dir", "data_dir"]


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "gans"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "gans"


class DeliveryMode(Enum):
    """How a selected code is delivered to the focused field from Quick Search."""

    #: Synthesize the code's characters directly (no clipboard change). Default.
    TYPE = "type"
    #: Copy to the clipboard, then synthesize Ctrl+V.
    PASTE = "paste"

    @property
    def label(self) -> str:
        return "Type the code" if self is DeliveryMode.TYPE else "Paste the code (Ctrl+V)"

    @classmethod
    def lenient(cls, value) -> "DeliveryMode":
        try:
            return cls(value)
        except (ValueError, TypeError):
            return cls.TYPE


class _Key:
    hotkey = "quickSearchHotkey"
    delivery_mode = "deliveryMode"
    also_copy_when_typing = "alsoCopyWhenTyping"
    clear_clipboard_enabled = "clearClipboardEnabled"
    clear_clipboard_seconds = "clearClipboardSeconds"
    recently_used_ids = "recentlyUsedIDs"
    usage_counts = "usageCounts"
    last_used_at = "lastUsedAt"
    require_unlock = "requireUnlock"
    show_codes_in_quick_search = "showCodesInQuickSearch"
    honk_on_copy = "honkOnCopy"
    has_completed_onboarding = "hasCompletedOnboarding"
    update_checks_enabled = "updateChecksEnabled"
    update_last_check = "updateLastCheck"
    update_skipped_version = "updateSkippedVersion"


class Preferences:
    """Observable settings. Every setter persists immediately (atomic write) and notifies
    observers synchronously on the calling thread — the UI mutates preferences from the
    main thread only."""

    #: How many recently-used entry ids to remember (for Quick Search ordering).
    RECENT_LIMIT = 50

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else config_dir() / "preferences.json"
        self._lock = threading.RLock()
        self._observers: List[Callable[[], None]] = []
        self._values: Dict[str, object] = self._load()

    # MARK: Storage

    def _load(self) -> Dict[str, object]:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        with self._lock:
            snapshot = dict(self._values)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self._path.with_suffix(".json.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2, sort_keys=True)
            os.replace(temporary, self._path)
        except OSError as error:
            log.app.error("Couldn't save preferences: %s", error)

    def _get(self, key: str, default):
        with self._lock:
            return self._values.get(key, default)

    def _set(self, key: str, value) -> None:
        with self._lock:
            self._values[key] = value
        self.save()
        self._notify()

    # MARK: Observers

    def on_change(self, callback: Callable[[], None]) -> None:
        self._observers.append(callback)

    def _notify(self) -> None:
        for callback in list(self._observers):
            try:
                callback()
            except Exception:  # pragma: no cover - observers must not break persistence
                log.app.exception("Preferences observer failed")

    # MARK: Typed accessors

    @property
    def hotkey(self) -> HotkeySpec:
        return HotkeySpec.from_json(self._get(_Key.hotkey, None)) or HotkeySpec.DEFAULT

    @hotkey.setter
    def hotkey(self, value: HotkeySpec) -> None:
        self._set(_Key.hotkey, value.to_json())

    @property
    def delivery_mode(self) -> DeliveryMode:
        """How Quick Search delivers a code to the focused app."""
        return DeliveryMode.lenient(self._get(_Key.delivery_mode, "type"))

    @delivery_mode.setter
    def delivery_mode(self, value: DeliveryMode) -> None:
        self._set(_Key.delivery_mode, value.value)

    @property
    def also_copy_when_typing(self) -> bool:
        """When typing the code, also place it on the clipboard as a convenience."""
        return bool(self._get(_Key.also_copy_when_typing, True))

    @also_copy_when_typing.setter
    def also_copy_when_typing(self, value: bool) -> None:
        self._set(_Key.also_copy_when_typing, bool(value))

    @property
    def clear_clipboard_enabled(self) -> bool:
        """Clear a copied code from the clipboard after a delay (only if it's still there)."""
        return bool(self._get(_Key.clear_clipboard_enabled, True))

    @clear_clipboard_enabled.setter
    def clear_clipboard_enabled(self, value: bool) -> None:
        self._set(_Key.clear_clipboard_enabled, bool(value))

    @property
    def clear_clipboard_seconds(self) -> int:
        """How long to wait before clearing a copied code (seconds)."""
        value = self._get(_Key.clear_clipboard_seconds, 30)
        return int(value) if isinstance(value, (int, float)) else 30

    @clear_clipboard_seconds.setter
    def clear_clipboard_seconds(self, value: int) -> None:
        self._set(_Key.clear_clipboard_seconds, int(value))

    @property
    def require_unlock(self) -> bool:
        """Require the user's password (polkit) to unlock Gans on launch (and via Lock Now)."""
        return bool(self._get(_Key.require_unlock, False))

    @require_unlock.setter
    def require_unlock(self, value: bool) -> None:
        self._set(_Key.require_unlock, bool(value))

    @property
    def show_codes_in_quick_search(self) -> bool:
        """Reveal the live code in each Quick Search row. Off by default — Quick Search just
        types the selected code without ever displaying it."""
        return bool(self._get(_Key.show_codes_in_quick_search, False))

    @show_codes_in_quick_search.setter
    def show_codes_in_quick_search(self, value: bool) -> None:
        self._set(_Key.show_codes_in_quick_search, bool(value))

    @property
    def honk_on_copy(self) -> bool:
        """🪿 Play a little honk (and blink the tray key into a goose) when a code is copied
        or typed. Off by default, obviously."""
        return bool(self._get(_Key.honk_on_copy, False))

    @honk_on_copy.setter
    def honk_on_copy(self, value: bool) -> None:
        self._set(_Key.honk_on_copy, bool(value))

    @property
    def has_completed_onboarding(self) -> bool:
        """Set once the first-run welcome has been shown, so it never repeats."""
        return bool(self._get(_Key.has_completed_onboarding, False))

    @has_completed_onboarding.setter
    def has_completed_onboarding(self, value: bool) -> None:
        self._set(_Key.has_completed_onboarding, bool(value))

    # MARK: Update checker state (namespaced like the macOS UpdateChecker's defaults)

    @property
    def update_checks_enabled(self) -> bool:
        return bool(self._get(_Key.update_checks_enabled, True))

    @update_checks_enabled.setter
    def update_checks_enabled(self, value: bool) -> None:
        self._set(_Key.update_checks_enabled, bool(value))

    @property
    def update_last_check(self) -> Optional[float]:
        value = self._get(_Key.update_last_check, None)
        return float(value) if isinstance(value, (int, float)) else None

    @update_last_check.setter
    def update_last_check(self, value: Optional[float]) -> None:
        self._set(_Key.update_last_check, value)

    @property
    def update_skipped_version(self) -> Optional[str]:
        value = self._get(_Key.update_skipped_version, None)
        return value if isinstance(value, str) else None

    @update_skipped_version.setter
    def update_skipped_version(self, value: Optional[str]) -> None:
        self._set(_Key.update_skipped_version, value)

    # MARK: Recently used / frecency

    @property
    def recently_used_ids(self) -> List[str]:
        """Most-recently-used entry ids, most recent first. Drives Quick Search ordering."""
        value = self._get(_Key.recently_used_ids, [])
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    @property
    def usage_counts(self) -> Dict[str, int]:
        """Per-entry lifetime use counts, for frecency ordering + the Settings "most used"."""
        value = self._get(_Key.usage_counts, {})
        if not isinstance(value, dict):
            return {}
        return {key: int(count) for key, count in value.items() if isinstance(key, str) and isinstance(count, (int, float))}

    @property
    def _last_used_at(self) -> Dict[str, float]:
        value = self._get(_Key.last_used_at, {})
        if not isinstance(value, dict):
            return {}
        return {key: float(stamp) for key, stamp in value.items() if isinstance(key, str) and isinstance(stamp, (int, float))}

    def record_usage(self, entry_id: str) -> None:
        """Records that ``entry_id`` was just used: moves it to the front of the recency list,
        bumps its lifetime count, and stamps the time — feeding both recency and frecency."""
        with self._lock:
            ids = [item for item in self.recently_used_ids if item != entry_id]
            ids.insert(0, entry_id)
            ids = ids[: self.RECENT_LIMIT]
            counts = self.usage_counts
            counts[entry_id] = counts.get(entry_id, 0) + 1
            stamps = self._last_used_at
            stamps[entry_id] = _time.time()
            self._values[_Key.recently_used_ids] = ids
            self._values[_Key.usage_counts] = counts
            self._values[_Key.last_used_at] = stamps
        self.save()
        self._notify()

    @property
    def frecency_ranked_ids(self) -> List[str]:
        """Entry ids ranked by **frecency** — frequency tempered by recency, so a code you use
        often *and* lately floats highest. Entries never used aren't listed (they fall back
        to name order downstream)."""
        now = _time.time()
        counts = self.usage_counts
        stamps = self._last_used_at

        def score(entry_id: str) -> float:
            count = float(counts.get(entry_id, 0))
            age_days = max(0.0, (now - stamps.get(entry_id, 0.0)) / 86_400)
            return count / (1.0 + age_days)  # frequency, decayed by age

        return sorted(counts.keys(), key=lambda entry_id: (-score(entry_id), -stamps.get(entry_id, 0.0)))

    def most_used(self, limit: int = 5) -> List[Tuple[str, int]]:
        """The most-used entry ids with their counts, highest first — for a Settings summary."""
        ranked = sorted(self.usage_counts.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:limit]

    @property
    def clipboard_clear_delay(self) -> Optional[float]:
        """The clipboard-clear delay, or ``None`` when the feature is off — what callers pass
        to the code injector."""
        return float(self.clear_clipboard_seconds) if self.clear_clipboard_enabled else None

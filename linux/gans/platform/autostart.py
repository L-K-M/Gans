""""Launch at login" through XDG autostart — the counterpart of ``LaunchAtLogin.swift``
(``SMAppService`` on macOS).

Every freedesktop session runs the ``.desktop`` files in ``$XDG_CONFIG_HOME/autostart``
at login, so enabling means writing ``ch.lkmc.Gans.desktop`` there and disabling means
deleting it. A file the user (or a desktop's Startup Applications panel) has switched off
with ``Hidden=true`` or ``X-GNOME-Autostart-enabled=false`` counts as disabled too, so
Settings reflects what the desktop will actually do.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .. import log

__all__ = ["LaunchAtLogin"]


class LaunchAtLogin:
    FILE_NAME = "ch.lkmc.Gans.desktop"
    CONTENT = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Gans\n"
        "Comment=Ente Auth codes, one keystroke away\n"
        "Exec=gans\n"
        "Icon=ch.lkmc.Gans\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "X-GNOME-Autostart-enabled=true\n"
        "StartupNotify=false\n"
    )

    @staticmethod
    def path() -> Path:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        return Path(base) / "autostart" / LaunchAtLogin.FILE_NAME

    # MARK: State

    @classmethod
    def is_enabled(cls) -> bool:
        """True when the autostart entry exists and hasn't been switched off in place."""
        try:
            text = cls.path().read_text(encoding="utf-8")
        except OSError:
            return False
        entries = cls._desktop_entry_keys(text)
        if entries.get("hidden", "").lower() == "true":
            return False
        if entries.get("x-gnome-autostart-enabled", "").lower() == "false":
            return False
        return True

    @classmethod
    def set(cls, enabled: bool) -> None:
        """Writes or removes the autostart entry. Failures are logged, not raised, like
        the macOS toggle."""
        path = cls.path()
        try:
            if enabled:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic replace so a crash mid-write can't leave a half desktop file.
                handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                                     prefix=".gans-autostart-", delete=False)
                with handle:
                    handle.write(cls.CONTENT)
                os.chmod(handle.name, 0o644)
                os.replace(handle.name, path)
            elif path.exists():
                path.unlink()
        except OSError as error:
            log.app.error("Launch-at-login toggle failed: %s", error)

    # MARK: Parsing

    @staticmethod
    def _desktop_entry_keys(text: str) -> dict:
        """The ``[Desktop Entry]`` group's keys, lower-cased (values as written). Hand-rolled
        rather than ``configparser`` because desktop files allow ``Name[de]=`` keys and
        repeated entries that the INI parser rejects."""
        keys: dict = {}
        in_group = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                in_group = line == "[Desktop Entry]"
                continue
            if in_group and "=" in line:
                key, _, value = line.partition("=")
                keys.setdefault(key.strip().lower(), value.strip())
        return keys

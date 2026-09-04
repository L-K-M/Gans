"""Persists the *already-encrypted* authenticator entities Ente returns, plus the diff
cursor, in ``$XDG_DATA_HOME/gans/``. These blobs are useless without the authenticator key
(kept in the Secret Service), so the cache is safe at rest and lets the menu populate
instantly/offline at launch before a network refresh completes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .. import log
from ..prefs import data_dir

__all__ = ["CachedEntity", "Snapshot", "EntityCache"]


@dataclass
class CachedEntity:
    id: str
    encrypted_data: str
    header: str


@dataclass
class Snapshot:
    entities: List[CachedEntity] = field(default_factory=list)
    since_time: int = 0


class EntityCache:
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path is not None else data_dir() / "entities.json"

    def load(self) -> Snapshot:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return Snapshot()
        if not isinstance(raw, dict):
            return Snapshot()
        entities: List[CachedEntity] = []
        for item in raw.get("entities", []) or []:
            if not isinstance(item, dict):
                continue
            entity_id, data, header = item.get("id"), item.get("encryptedData"), item.get("header")
            if isinstance(entity_id, str) and isinstance(data, str) and isinstance(header, str):
                entities.append(CachedEntity(entity_id, data, header))
        since = raw.get("sinceTime", 0)
        return Snapshot(entities, int(since) if isinstance(since, (int, float)) else 0)

    def save(self, snapshot: Snapshot) -> None:
        payload = {
            "entities": [{"id": e.id, "encryptedData": e.encrypted_data, "header": e.header} for e in snapshot.entities],
            "sinceTime": snapshot.since_time,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self._path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except OSError as error:
            log.ente.error("Couldn't save the entity cache: %s", error)

    def clear(self) -> None:
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        except OSError as error:
            log.ente.error("Couldn't clear the entity cache: %s", error)

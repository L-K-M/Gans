"""Session secrets: the Ente auth token, the 32-byte authenticator key, and a little session
metadata — never the password, and never plaintext TOTP secrets.

The Secret Service (GNOME Keyring, KWallet's bridge, KeePassXC…) is the Linux equivalent
of the macOS Keychain. When none is reachable the session lives in memory only; nothing
secret is ever written to disk in plaintext.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, Protocol

from .. import log

__all__ = ["Keyring", "MemoryKeyring", "SecretServiceKeyring", "open_keyring"]

_APPLICATION = "gans"
_LABELS = {
    "ente.token": "Gans — Ente session token",
    "ente.authKey": "Gans — Ente authenticator key",
    "ente.email": "Gans — Ente account email",
}


class Keyring(Protocol):
    persistent: bool

    def get(self, account: str) -> Optional[bytes]: ...

    def set(self, account: str, data: bytes) -> bool: ...

    def remove(self, account: str) -> bool: ...


class MemoryKeyring:
    """Process-lifetime storage, used when no Secret Service is available."""

    persistent = False

    def __init__(self) -> None:
        self._items: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    def get(self, account: str) -> Optional[bytes]:
        with self._lock:
            return self._items.get(account)

    def set(self, account: str, data: bytes) -> bool:
        with self._lock:
            self._items[account] = bytes(data)
        return True

    def remove(self, account: str) -> bool:
        with self._lock:
            self._items.pop(account, None)
        return True


class SecretServiceKeyring:
    """Items live in the default collection with ``application=gans`` + ``account=<key>``
    attributes and a readable label, so they're inspectable in Seahorse/KWalletManager."""

    persistent = True

    def __init__(self, connection, collection) -> None:
        self._connection = connection
        self._collection = collection
        self._lock = threading.Lock()

    @classmethod
    def connect(cls) -> "SecretServiceKeyring":
        import secretstorage  # python3-secretstorage

        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        if collection.is_locked():
            # Prompts through the desktop's keyring dialog; raises if the user declines.
            collection.unlock()
            if collection.is_locked():
                raise RuntimeError("The default keyring collection is locked.")
        return cls(connection, collection)

    def _attributes(self, account: str) -> Dict[str, str]:
        return {"application": _APPLICATION, "account": account}

    def _find(self, account: str):
        return list(self._collection.search_items(self._attributes(account)))

    def get(self, account: str) -> Optional[bytes]:
        with self._lock:
            try:
                for item in self._find(account):
                    if item.is_locked():
                        item.unlock()
                    return bytes(item.get_secret())
                return None
            except Exception as error:
                log.app.error("Secret Service read failed: %s", error)
                return None

    def set(self, account: str, data: bytes) -> bool:
        with self._lock:
            try:
                for stale in self._find(account):
                    stale.delete()
                self._collection.create_item(_LABELS.get(account, f"Gans — {account}"),
                                             self._attributes(account), bytes(data), replace=True)
                return True
            except Exception as error:
                log.app.error("Secret Service write failed: %s", error)
                return False

    def remove(self, account: str) -> bool:
        with self._lock:
            try:
                for item in self._find(account):
                    item.delete()
                return True
            except Exception as error:
                log.app.error("Secret Service delete failed: %s", error)
                return False


def open_keyring() -> Keyring:
    """The Secret Service when it's reachable, otherwise a memory-only fallback."""
    try:
        keyring = SecretServiceKeyring.connect()
        log.app.info("Using the Secret Service keyring")
        return keyring
    except ImportError:
        log.app.warning("python3-secretstorage is not installed; the session won't be remembered across launches")
    except Exception as error:
        log.app.warning("No Secret Service available (%s); the session won't be remembered across launches", error)
    return MemoryKeyring()

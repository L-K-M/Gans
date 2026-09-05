import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from gans.store.cache import CachedEntity, EntityCache, Snapshot
from gans.store.keyring import MemoryKeyring, SecretServiceKeyring, open_keyring


class EntityCacheTests(unittest.TestCase):
    def test_round_trip_clear_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "entities.json"
            cache = EntityCache(path)
            self.assertEqual(cache.load().entities, [])
            cache.save(Snapshot([CachedEntity("1", "data", "hdr")], 99))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            loaded = cache.load()
            self.assertEqual(loaded.since_time, 99)
            self.assertEqual(loaded.entities[0].encrypted_data, "data")
            cache.clear()
            cache.clear()  # idempotent
            self.assertEqual(cache.load().entities, [])

    def test_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entities.json"
            path.write_text("[1,2")
            self.assertEqual(EntityCache(path).load().since_time, 0)
            path.write_text('{"entities": [1, {"id": "a"}], "sinceTime": "x"}')
            self.assertEqual(EntityCache(path).load().entities, [])


class MemoryKeyringTests(unittest.TestCase):
    def test_set_get_remove(self):
        keyring = MemoryKeyring()
        self.assertFalse(keyring.persistent)
        self.assertIsNone(keyring.get("a"))
        keyring.set("a", b"1")
        self.assertEqual(keyring.get("a"), b"1")
        keyring.remove("a")
        keyring.remove("a")
        self.assertIsNone(keyring.get("a"))


# MARK: Secret Service (fake daemon)

class FakeSecretService:
    """Stands in for the ``secretstorage`` module: items live in the "daemon" across
    connections, and ``restart()`` invalidates every session opened before it — which
    is what a real gnome-keyring restart does (``org.freedesktop.Secret.Error.NoSession``
    on every call over the old connection)."""

    class NotAvailable(Exception):
        pass

    class NoSession(Exception):
        pass

    def __init__(self):
        self.items = {}            # (application, account) -> (label, secret)
        self.generation = 0
        self.available = True
        self.locked = False
        self.refuse_unlock = False
        self.unlock_calls = 0
        self.connections = []

    def restart(self):
        self.generation += 1

    # The module surface SecretServiceKeyring uses.

    def dbus_init(self):
        if not self.available:
            raise self.NotAvailable("the name org.freedesktop.secrets was not provided")
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def get_default_collection(self, connection):
        connection.check()
        return FakeCollection(connection)

    def as_module(self):
        module = types.ModuleType("secretstorage")
        module.dbus_init = self.dbus_init
        module.get_default_collection = self.get_default_collection
        module.SecretServiceNotAvailableException = self.NotAvailable
        return module


class FakeConnection:
    def __init__(self, daemon):
        self.daemon = daemon
        self.generation = daemon.generation
        self.closed = False

    def check(self):
        if not self.daemon.available:
            raise self.daemon.NotAvailable("the name org.freedesktop.secrets was not provided")
        if self.generation != self.daemon.generation:
            raise self.daemon.NoSession("[org.freedesktop.Secret.Error.NoSession] The session does not exist")

    def close(self):
        self.closed = True


class FakeCollection:
    def __init__(self, connection):
        self.connection = connection
        self.daemon = connection.daemon

    def is_locked(self):
        self.connection.check()
        return self.daemon.locked

    def unlock(self):
        self.connection.check()
        self.daemon.unlock_calls += 1
        if not self.daemon.refuse_unlock:
            self.daemon.locked = False
        return self.daemon.refuse_unlock   # secretstorage returns "dismissed"

    def search_items(self, attributes):
        self.connection.check()
        key = (attributes["application"], attributes["account"])
        return [FakeItem(self.connection, key)] if key in self.daemon.items else []

    def create_item(self, label, attributes, secret, replace=False):
        self.connection.check()
        self.daemon.items[(attributes["application"], attributes["account"])] = (label, bytes(secret))


class FakeItem:
    def __init__(self, connection, key):
        self.connection = connection
        self.key = key

    def is_locked(self):
        self.connection.check()
        return False

    def get_secret(self):
        self.connection.check()
        return self.connection.daemon.items[self.key][1]

    def delete(self):
        self.connection.check()
        self.connection.daemon.items.pop(self.key, None)


class SecretServiceKeyringTests(unittest.TestCase):
    def setUp(self):
        self.daemon = FakeSecretService()
        patcher = mock.patch.dict(sys.modules, {"secretstorage": self.daemon.as_module()})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keyring = SecretServiceKeyring.connect()
        self.assertTrue(self.keyring.set("ente.token", b"tok"))

    def test_round_trip_labels_and_attributes(self):
        self.assertTrue(self.keyring.persistent)
        self.assertEqual(self.keyring.get("ente.token"), b"tok")
        self.assertEqual(self.daemon.items[("gans", "ente.token")], ("Gans — Ente session token", b"tok"))
        self.assertIsNone(self.keyring.get("ente.authKey"))
        self.assertTrue(self.keyring.remove("ente.token"))
        self.assertIsNone(self.keyring.get("ente.token"))
        self.assertEqual(len(self.daemon.connections), 1)

    def test_reconnects_after_the_daemon_restarts(self):
        # A restarted daemon doesn't know the old session; without a fresh connection
        # every read fails and the app looks signed out until relaunch.
        self.daemon.restart()
        self.assertEqual(self.keyring.get("ente.token"), b"tok")
        self.assertEqual(len(self.daemon.connections), 2)
        self.assertTrue(self.daemon.connections[0].closed)
        # The new connection is kept: no reconnect per call.
        self.assertEqual(self.keyring.get("ente.token"), b"tok")
        self.assertEqual(len(self.daemon.connections), 2)

    def test_writes_and_deletes_reconnect_too(self):
        self.daemon.restart()
        self.assertTrue(self.keyring.set("ente.email", b"a@b.c"))
        self.assertEqual(self.daemon.items[("gans", "ente.email")][1], b"a@b.c")
        self.daemon.restart()
        self.assertTrue(self.keyring.remove("ente.token"))
        self.assertNotIn(("gans", "ente.token"), self.daemon.items)
        self.assertEqual(len(self.daemon.connections), 3)

    def test_reconnect_unlocks_a_relocked_collection(self):
        self.daemon.restart()
        self.daemon.locked = True
        self.assertEqual(self.keyring.get("ente.token"), b"tok")
        self.assertEqual(self.daemon.unlock_calls, 1)
        # ...but a declined unlock is a plain failure, not an exception.
        self.daemon.restart()
        self.daemon.locked = True
        self.daemon.refuse_unlock = True
        self.assertIsNone(self.keyring.get("ente.token"))
        self.assertFalse(self.keyring.set("ente.token", b"x"))

    def test_gives_up_when_the_service_is_gone(self):
        self.daemon.available = False
        self.assertIsNone(self.keyring.get("ente.token"))
        self.assertFalse(self.keyring.set("ente.token", b"x"))
        self.assertFalse(self.keyring.remove("ente.token"))
        self.assertEqual(len(self.daemon.connections), 1)   # dbus_init refused; nothing leaked
        # Back (e.g. restarted by the session): the next call recovers.
        self.daemon.available = True
        self.daemon.restart()
        self.assertEqual(self.keyring.get("ente.token"), b"tok")

    def test_open_keyring_falls_back_to_memory_without_a_service(self):
        self.daemon.available = False
        self.assertIsInstance(open_keyring(), MemoryKeyring)
        self.daemon.available = True
        self.assertIsInstance(open_keyring(), SecretServiceKeyring)


# MARK: Secret Service (real gnome-keyring-daemon)

def _gnome_keyring_usable() -> bool:
    if not (shutil.which("gnome-keyring-daemon") and shutil.which("dbus-daemon") and shutil.which("Xvfb")):
        return False
    try:
        import secretstorage  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_gnome_keyring_usable(), "needs gnome-keyring-daemon, dbus-daemon, Xvfb and secretstorage")
class GnomeKeyringIntegrationTests(unittest.TestCase):
    """The reconnect path against the real daemon on a private bus: kill it, start a new
    one, and the keyring opened before must keep working."""

    @classmethod
    def setUpClass(cls):
        from tests.harness import DisplaySession
        cls.session = DisplaySession.start()
        cls.tmp = tempfile.TemporaryDirectory()
        home = Path(cls.tmp.name)
        (home / "run").mkdir(mode=0o700)
        keyrings = home / "data" / "keyrings"
        keyrings.mkdir(parents=True, mode=0o700)
        # An unencrypted "gans" keyring as the default: no unlock prompt can ever appear.
        (keyrings / "default").write_text("gans\n")
        (keyrings / "gans.keyring").write_text("[keyring]\ndisplay-name=Gans Test\nctime=0\nmtime=0\n"
                                              "lock-on-idle=false\nlock-after=false\n")
        cls.env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / "data"),
                       XDG_RUNTIME_DIR=str(home / "run"), XDG_CONFIG_HOME=str(home / "config"))
        cls.daemon = None

    @classmethod
    def tearDownClass(cls):
        cls._stop_daemon()
        cls.session.stop()
        cls.tmp.cleanup()

    @classmethod
    def _start_daemon(cls):
        cls.daemon = subprocess.Popen(["gnome-keyring-daemon", "--foreground", "--components=secrets"],
                                      env=cls.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                connection, _collection = SecretServiceKeyring._open()
            except Exception:
                time.sleep(0.1)
                continue
            connection.close()
            return
        raise unittest.SkipTest("gnome-keyring-daemon did not come up on the private bus")

    @classmethod
    def _stop_daemon(cls):
        if cls.daemon is not None and cls.daemon.poll() is None:
            cls.daemon.terminate()
            try:
                cls.daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.daemon.kill()
                cls.daemon.wait()
        cls.daemon = None

    def test_keyring_survives_a_daemon_restart(self):
        self._start_daemon()
        keyring = open_keyring()
        self.assertIsInstance(keyring, SecretServiceKeyring)
        self.assertTrue(keyring.set("ente.token", b"tok-1"))
        self.assertEqual(keyring.get("ente.token"), b"tok-1")

        # Gone: fail fast (the private bus can't auto-start anything), no exception.
        self._stop_daemon()
        self.assertIsNone(keyring.get("ente.token"))
        self.assertFalse(keyring.set("ente.token", b"tok-2"))

        # Back: the old session is dead, the keyring reconnects and reads the item again.
        self._start_daemon()
        self.assertEqual(keyring.get("ente.token"), b"tok-1")
        self.assertTrue(keyring.set("ente.token", b"tok-2"))
        self.assertEqual(keyring.get("ente.token"), b"tok-2")
        self.assertTrue(keyring.remove("ente.token"))
        self.assertIsNone(keyring.get("ente.token"))


if __name__ == "__main__":
    unittest.main()

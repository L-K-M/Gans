import os
import tempfile
import unittest
from pathlib import Path

from gans.store.cache import CachedEntity, EntityCache, Snapshot
from gans.store.keyring import MemoryKeyring


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


if __name__ == "__main__":
    unittest.main()

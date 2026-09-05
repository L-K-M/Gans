"""AppLock against fake ``pkcheck`` scripts (selected through ``GANS_PKCHECK``), covering
the authorized / refused / missing / crashed outcomes and the re-entrancy guard."""

import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gans.platform.applock import AppLock
from gans.prefs import Preferences


class AppLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.prefs = Preferences(self.root / "preferences.json")
        self.prefs.require_unlock = True
        self.changes = 0

    def lock(self, locked=True):
        lock = AppLock(self.prefs, lambda fn: fn())
        lock.on_change(self.on_change)
        if locked:
            lock.lock_if_enabled()
        return lock

    def on_change(self):
        self.changes += 1

    def fake_pkcheck(self, body):
        """Writes a fake pkcheck that records its argv to ``args.txt`` and then runs ``body``."""
        script = self.root / "pkcheck"
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > \"{self.root / 'args.txt'}\"\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return patch.dict(os.environ, {"GANS_PKCHECK": str(script)})

    def authenticate(self, lock, timeout=10.0):
        done = threading.Event()
        outcome = []

        def completion(ok):
            outcome.append(ok)
            done.set()

        lock.authenticate(completion=completion)
        self.assertTrue(done.wait(timeout), "authentication never completed")
        return outcome[0]

    # MARK: State

    def test_lock_if_enabled_follows_the_preference(self):
        self.prefs.require_unlock = False
        lock = self.lock()
        self.assertFalse(lock.is_locked)
        self.assertFalse(lock.is_enabled)
        self.prefs.require_unlock = True
        lock.lock_if_enabled()
        self.assertTrue(lock.is_locked)
        self.assertTrue(lock.is_enabled)
        self.assertEqual(self.changes, 1)

    def test_lock_now_ignores_the_preference(self):
        self.prefs.require_unlock = False
        lock = self.lock(locked=False)
        lock.lock()
        self.assertTrue(lock.is_locked)
        lock.lock()   # already locked: observers aren't spammed
        self.assertEqual(self.changes, 1)

    def test_unlocked_completes_immediately_without_pkcheck(self):
        with self.fake_pkcheck("exit 0"):
            lock = self.lock(locked=False)
            self.assertTrue(self.authenticate(lock))
        self.assertFalse((self.root / "args.txt").exists())

    # MARK: pkcheck outcomes

    def test_authorized_unlocks(self):
        with self.fake_pkcheck("exit 0"):
            lock = self.lock()
            self.assertTrue(self.authenticate(lock))
        self.assertFalse(lock.is_locked)
        self.assertEqual(self.changes, 2)
        args = (self.root / "args.txt").read_text().split()
        self.assertEqual(args, ["--action-id", "ch.lkmc.gans.unlock", "--process", str(os.getpid()),
                                "--allow-user-interaction"])

    def test_not_authorized_stays_locked(self):
        with self.fake_pkcheck('echo "Not authorized." >&2; exit 1'):
            lock = self.lock()
            self.assertFalse(self.authenticate(lock))
        self.assertTrue(lock.is_locked)
        self.assertEqual(self.changes, 1)

    def test_dismissed_prompt_stays_locked(self):
        with self.fake_pkcheck('echo "Authentication request was dismissed." >&2; exit 1'):
            lock = self.lock()
            self.assertFalse(self.authenticate(lock))
        self.assertTrue(lock.is_locked)

    def test_missing_pkcheck_unlocks_with_a_warning(self):
        with patch.dict(os.environ, {"GANS_PKCHECK": str(self.root / "missing" / "pkcheck")}):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING"):
                self.assertTrue(self.authenticate(lock))
        self.assertFalse(lock.is_locked)

    def test_no_pkcheck_on_path_unlocks(self):
        with patch.dict(os.environ, {"GANS_PKCHECK": ""}), patch("gans.platform.applock.shutil.which", return_value=None):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING"):
                self.assertTrue(self.authenticate(lock))
        self.assertFalse(lock.is_locked)

    def test_unregistered_action_unlocks(self):
        body = ('echo "Error checking for authorization ch.lkmc.gans.unlock: '
                'Action ch.lkmc.gans.unlock is not registered" >&2; exit 127')
        with self.fake_pkcheck(body):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING") as logs:
                self.assertTrue(self.authenticate(lock))
        self.assertIn("not registered", logs.output[0])
        self.assertFalse(lock.is_locked)

    def test_no_authentication_agent_unlocks(self):
        with self.fake_pkcheck('echo "Authorization requires authentication but no agent is available." >&2; exit 2'):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING"):
                self.assertTrue(self.authenticate(lock))
        self.assertFalse(lock.is_locked)

    def test_crashing_pkcheck_unlocks(self):
        with self.fake_pkcheck("kill -SEGV $$"):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING"):
                self.assertTrue(self.authenticate(lock))
        self.assertFalse(lock.is_locked)

    def test_unanswered_prompt_stays_locked(self):
        with self.fake_pkcheck("sleep 5; exit 0"), patch.object(AppLock, "PROMPT_TIMEOUT", 0.3):
            lock = self.lock()
            with self.assertLogs("gans.app", level="WARNING"):
                self.assertFalse(self.authenticate(lock))
        self.assertTrue(lock.is_locked)

    def test_undecodable_agent_output_is_not_fatal(self):
        # A localized message in another encoding used to raise UnicodeDecodeError on the
        # worker: the completion never fired and the re-entrancy guard stayed engaged.
        with self.fake_pkcheck("printf 'Not authorized \\377\\376\\n' >&2; exit 1"):
            lock = self.lock()
            self.assertFalse(self.authenticate(lock))
        self.assertTrue(lock.is_locked)
        with self.fake_pkcheck("exit 0"):
            self.assertTrue(self.authenticate(lock))   # a new prompt is allowed again
        self.assertFalse(lock.is_locked)

    def test_worker_exception_still_completes_and_stays_locked(self):
        with patch.object(AppLock, "_check_authorization", side_effect=RuntimeError("boom")):
            lock = self.lock()
            with self.assertLogs("gans.app", level="ERROR"):
                self.assertFalse(self.authenticate(lock))
        self.assertTrue(lock.is_locked)
        self.assertEqual(self.changes, 1)
        with self.fake_pkcheck("exit 0"):
            self.assertTrue(self.authenticate(lock))   # the guard was released
        self.assertFalse(lock.is_locked)

    def test_reentrant_prompt_is_refused_while_one_is_showing(self):
        with self.fake_pkcheck("sleep 0.5; exit 0"):
            lock = self.lock()
            first = threading.Event()
            results = []
            lock.authenticate(completion=lambda ok: (results.append(("first", ok)), first.set()))
            lock.authenticate(completion=lambda ok: results.append(("second", ok)))
            self.assertEqual(results, [("second", False)])
            self.assertTrue(first.wait(10))
        self.assertEqual(results, [("second", False), ("first", True)])
        self.assertFalse(lock.is_locked)
        self.assertTrue(self.authenticate(lock))   # a new prompt is allowed again (and trivially true)

    def test_classification_table(self):
        classify = AppLock._classify
        self.assertEqual(classify(0, ""), (True, None))
        self.assertEqual(classify(1, "Not authorized.\n"), (False, None))
        self.assertEqual(classify(1, "pkcheck: authorization failed"), (False, None))
        self.assertEqual(classify(3, "Authentication request was dismissed."), (False, None))
        unlock, warning = classify(1, "pkcheck: unrecognized option")
        self.assertTrue(unlock)
        self.assertIn("exited 1", warning)
        unlock, warning = classify(126, "Error getting authority: Could not connect")
        self.assertTrue(unlock)
        self.assertIn("Could not connect", warning)
        unlock, warning = classify(-11, "")
        self.assertTrue(unlock)
        self.assertIn("no output", warning)


if __name__ == "__main__":
    unittest.main()

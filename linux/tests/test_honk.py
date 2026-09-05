"""Honk synthesis (WAV container, determinism, envelope) and the playback fallbacks."""

import io
import os
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from gans.platform.honk import Honk

SAMPLE_COUNT = int(22_050 * 0.34)   # 7497

#: Pinned outputs of the ported synthesis (index → sample), so a drift in the formula shows.
KNOWN_SAMPLES = {0: 0, 1: 4, 2: 18, 10: 197, 100: 465, 500: -3174, 1000: 5894, 2000: 235,
                 3748: -5114, 5000: 3397, 7000: 740, 7495: -3, 7496: 0}


def samples_of(data):
    body = data[44:]
    return struct.unpack(f"<{len(body) // 2}h", body)


class HonkSynthesisTests(unittest.TestCase):
    def test_riff_wave_header(self):
        data = Honk.wav_bytes()
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(struct.unpack("<I", data[4:8])[0], len(data) - 8)
        self.assertEqual(data[8:16], b"WAVEfmt ")
        chunk_size, audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack("<IHHIIHH", data[16:36])
        self.assertEqual((chunk_size, audio_format, channels, rate, byte_rate, block_align, bits),
                         (16, 1, 1, 22_050, 44_100, 2, 16))
        self.assertEqual(data[36:40], b"data")
        self.assertEqual(struct.unpack("<I", data[40:44])[0], 2 * SAMPLE_COUNT)
        self.assertEqual(len(data), 44 + 2 * SAMPLE_COUNT)

        parsed = wave.open(io.BytesIO(data))
        self.assertEqual((parsed.getnchannels(), parsed.getsampwidth(), parsed.getframerate(), parsed.getnframes()),
                         (1, 2, 22_050, SAMPLE_COUNT))

    def test_deterministic_and_cached(self):
        first = Honk.wav_bytes()
        self.assertIs(Honk.wav_bytes(), first)
        self.assertEqual(Honk._wrap_wav(Honk._samples(), Honk.SAMPLE_RATE), first)

    def test_envelope(self):
        samples = samples_of(Honk.wav_bytes())
        self.assertEqual(samples[0], 0)                       # attack starts from silence
        self.assertTrue(any(samples[1:20]))                   # ...but is audible almost at once
        attack_peak = max(abs(value) for value in samples[:50])
        body_peak = max(abs(value) for value in samples[1000:2000])
        self.assertLess(attack_peak, body_peak)               # fast attack ramps up
        self.assertLess(abs(samples[-1]), 8)                  # gentle release ends near silence
        peak = max(abs(value) for value in samples)
        self.assertLessEqual(peak, 32_767)
        self.assertGreater(peak, 6_000)                       # 0.33 amplitude, four harmonics
        self.assertLess(peak, 11_000)

    def test_known_samples(self):
        samples = samples_of(Honk.wav_bytes())
        for index, expected in KNOWN_SAMPLES.items():
            self.assertEqual(samples[index], expected, index)


class HonkPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        state = patch.multiple(Honk, _wav_path=None, _playbin=None, _gst=None, _gst_unavailable=False,
                               _reported_unavailable=False)
        state.start()
        self.addCleanup(state.stop)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_play_without_any_backend_is_a_quiet_noop(self):
        with patch.object(Honk, "_gst_playbin", return_value=None), \
                patch("gans.platform.honk.shutil.which", return_value=None):
            with self.assertLogs("gans.app", level="DEBUG") as logs:
                self.assertIsNone(Honk.play())
            self.assertEqual(len(logs.output), 1)
            self.assertIn("No audio backend", logs.output[0])
            with self.assertNoLogs("gans.app", level="DEBUG"):
                Honk.play()   # reported once, not on every copy

    def test_cli_fallback_plays_the_wav_file(self):
        out = self.root / "played.txt"
        script = self.root / "paplay"
        script.write_text(f"#!/bin/sh\nprintf '%s' \"$1\" > \"{out}\"\n")
        script.chmod(0o755)
        with patch.object(Honk, "_gst_playbin", return_value=None), \
                patch("gans.platform.honk.shutil.which", side_effect=lambda name: str(script) if name == "paplay" else None):
            Honk.play()
        deadline = time.monotonic() + 5
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        played = Path(out.read_text())
        self.assertEqual(played, self.root / "gans-honk.wav")
        self.assertEqual(played.read_bytes(), Honk.wav_bytes())
        self.assertEqual(os.stat(played).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(self.root)), ["gans-honk.wav", "paplay", "played.txt"])  # no temp litter

    def test_cli_player_preference_order(self):
        def which(name):
            return f"/usr/bin/{name}" if name in available else None

        with patch("gans.platform.honk.shutil.which", side_effect=which):
            available = {"aplay", "ffplay"}
            self.assertEqual(Honk._cli_command(), ["/usr/bin/aplay"])
            available = {"ffplay"}
            self.assertEqual(Honk._cli_command(), ["/usr/bin/ffplay", "-nodisp", "-autoexit"])
            available = {"paplay", "pw-play", "aplay"}
            self.assertEqual(Honk._cli_command(), ["/usr/bin/paplay"])
            available = set()
            self.assertIsNone(Honk._cli_command())

    def test_wav_file_falls_back_to_the_temp_dir(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "missing")}):
            path = Honk._wav_file()
        self.assertEqual(path, Path(tempfile.gettempdir()) / f"gans-honk-{os.getuid()}.wav")
        self.assertEqual(path.read_bytes(), Honk.wav_bytes())
        self.assertIs(Honk._wav_file(), path)   # written once

    def test_gstreamer_path_does_not_raise(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import GLib, Gst  # noqa: F401
        except (ImportError, ValueError):
            raise unittest.SkipTest("GStreamer typelib not available")
        if Honk._gst_playbin() is None:
            raise unittest.SkipTest("GStreamer playbin not available")
        Honk.play()
        Honk.play()   # restarting a honk in flight is fine
        deadline = time.monotonic() + 0.5
        context = GLib.MainContext.default()
        while time.monotonic() < deadline:   # let bus messages (EOS / no-sink errors) dispatch
            while context.iteration(False):
                pass
            time.sleep(0.01)
        self.assertTrue((self.root / "gans-honk.wav").exists())
        Honk._drop_playbin()


if __name__ == "__main__":
    unittest.main()

"""🪿 A synthesized goose honk, built once in memory and played on demand — no bundled audio
asset. A buzzy, nasal tone with a rise-and-fall pitch contour, deliberately short and a
little silly. Opt-in (see ``Preferences.honk_on_copy``). Ported sample-for-sample from
``Honk.swift``.

Playback goes through GStreamer's ``playbin`` when the ``Gst`` typelib is present (the
WAV is written once to ``$XDG_RUNTIME_DIR`` and played by URI), else through the first
command-line player on ``PATH`` (``paplay`` / ``pw-play`` / ``aplay`` / ``ffplay``), run
detached. With neither, ``play`` is a logged no-op — a honk must never break a copy.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
from array import array
from pathlib import Path
from typing import List, Optional

from .. import log

__all__ = ["Honk"]


class Honk:
    SAMPLE_RATE = 22_050
    DURATION = 0.34
    #: Command-line fallbacks, in order of preference; the WAV path is appended.
    CLI_PLAYERS = (["paplay"], ["pw-play"], ["aplay"], ["ffplay", "-nodisp", "-autoexit"])

    _wav: Optional[bytes] = None
    _wav_path: Optional[Path] = None
    _gst = None            # the Gst module once imported
    _playbin = None
    _gst_unavailable = False
    _reported_unavailable = False

    # MARK: Playback

    @classmethod
    def play(cls) -> None:
        """Plays the honk (restarting it if one is already sounding). Non-blocking; never
        raises; a no-op when no audio backend exists."""
        try:
            if cls._play_with_gstreamer() or cls._play_with_cli():
                return
            if not cls._reported_unavailable:
                cls._reported_unavailable = True
                log.app.debug("No audio backend for the honk (GStreamer playbin and %s all unavailable)",
                              ", ".join(player[0] for player in cls.CLI_PLAYERS))
        except Exception:
            log.app.exception("Honk failed")

    @classmethod
    def _play_with_gstreamer(cls) -> bool:
        playbin = cls._gst_playbin()
        if playbin is None:
            return False
        path = cls._wav_file()
        if path is None:
            return False
        gst = cls._gst
        playbin.set_state(gst.State.NULL)   # restart if already sounding
        playbin.set_property("uri", gst.filename_to_uri(str(path)))
        if playbin.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            log.app.debug("GStreamer refused to play the honk; using a command-line player")
            cls._drop_playbin()
            return False
        return True

    @classmethod
    def _gst_playbin(cls):
        """The shared ``playbin`` (created on first use), or None when GStreamer isn't
        usable here. ``Gst.init`` runs once; the bus watch resets the pipeline on EOS
        and drops GStreamer for good on an error (e.g. no audio sink) so later honks go
        straight to the fallback."""
        if cls._playbin is not None:
            return cls._playbin
        if cls._gst_unavailable:
            return None
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import GLib, Gst
        except (ImportError, ValueError) as error:
            log.app.debug("GStreamer not available for the honk: %s", error)
            cls._gst_unavailable = True
            return None
        try:
            if not Gst.is_initialized():
                Gst.init(None)
            playbin = Gst.ElementFactory.make("playbin", "gans-honk")
        except GLib.Error as error:
            log.app.debug("GStreamer couldn't initialize: %s", error.message)
            cls._gst_unavailable = True
            return None
        if playbin is None:
            log.app.debug("GStreamer has no playbin (gstreamer1.0-plugins-base missing)")
            cls._gst_unavailable = True
            return None
        bus = playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", cls._on_gst_eos)
        bus.connect("message::error", cls._on_gst_error)
        cls._gst = Gst
        cls._playbin = playbin
        return playbin

    @classmethod
    def _on_gst_eos(cls, _bus, _message) -> None:
        if cls._playbin is not None:
            cls._playbin.set_state(cls._gst.State.NULL)

    @classmethod
    def _on_gst_error(cls, _bus, message) -> None:
        error, _debug = message.parse_error()
        log.app.debug("GStreamer couldn't play the honk (%s); using a command-line player from now on",
                      error.message)
        cls._drop_playbin()

    @classmethod
    def _drop_playbin(cls) -> None:
        playbin, cls._playbin = cls._playbin, None
        if playbin is not None:
            playbin.get_bus().remove_signal_watch()
            playbin.set_state(cls._gst.State.NULL)
        cls._gst_unavailable = True

    @classmethod
    def _cli_command(cls) -> Optional[List[str]]:
        for player in cls.CLI_PLAYERS:
            found = shutil.which(player[0])
            if found:
                return [found] + list(player[1:])
        return None

    @classmethod
    def _play_with_cli(cls) -> bool:
        command = cls._cli_command()
        if command is None:
            return False
        path = cls._wav_file()
        if path is None:
            return False
        try:
            player = subprocess.Popen(command + [str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            log.app.debug("Couldn't start %s for the honk: %s", command[0], error)
            return False
        # Detached (own session, no pipes), but reaped as soon as it exits so it never
        # lingers as a zombie.
        threading.Thread(target=player.wait, name="gans-honk-reaper", daemon=True).start()
        return True

    @classmethod
    def _wav_file(cls) -> Optional[Path]:
        """The WAV on disk (written once per process, atomically, 0600). Lives in the
        per-user runtime dir; falls back to the temp dir with the uid in the name."""
        if cls._wav_path is not None and cls._wav_path.exists():
            return cls._wav_path
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir and os.access(runtime_dir, os.W_OK):
            path = Path(runtime_dir) / "gans-honk.wav"
        else:
            path = Path(tempfile.gettempdir()) / f"gans-honk-{os.getuid()}.wav"
        try:
            handle = tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".gans-honk-", delete=False)
            with handle:
                handle.write(cls.wav_bytes())
            os.chmod(handle.name, 0o600)
            os.replace(handle.name, path)
        except OSError as error:
            log.app.debug("Couldn't write the honk WAV to %s: %s", path, error)
            return None
        cls._wav_path = path
        return path

    # MARK: Synthesis

    @classmethod
    def wav_bytes(cls) -> bytes:
        """The synthesized honk as a 16-bit mono RIFF/WAVE file (cached, deterministic)."""
        if cls._wav is None:
            cls._wav = cls._wrap_wav(cls._samples(), cls.SAMPLE_RATE)
        return cls._wav

    @classmethod
    def _samples(cls) -> array:
        sample_rate = float(cls.SAMPLE_RATE)
        duration = cls.DURATION
        count = int(sample_rate * duration)
        samples = array("h")
        pi = math.pi

        for i in range(count):
            t = i / sample_rate
            frac = t / duration
            # Nasal pitch contour: a quick rise then settle, ~300–460 Hz.
            f0 = 300.0 + 160.0 * math.sin(pi * min(frac * 1.2, 1.0))
            # A few harmonics → buzzy, goose-ish timbre.
            s = math.sin(2 * pi * f0 * t)
            s += 0.5 * math.sin(2 * pi * 2 * f0 * t)
            s += 0.33 * math.sin(2 * pi * 3 * f0 * t)
            s += 0.2 * math.sin(2 * pi * 4 * f0 * t)
            # Envelope: fast attack, gentle release, slight two-syllable waver.
            attack = min(frac / 0.05, 1.0)
            release = max(0.0, 1.0 - (frac - 0.7) / 0.3) if frac > 0.7 else 1.0
            waver = 0.85 + 0.15 * math.cos(2 * pi * 3 * frac)
            amp = attack * release * waver * 0.33
            value = max(-1.0, min(1.0, s / 2.0 * amp))
            samples.append(int(value * 32_767))   # truncates toward zero, like Int16(_:)
        return samples

    @staticmethod
    def _wrap_wav(samples: array, sample_rate: int) -> bytes:
        """Wraps 16-bit mono PCM samples in a minimal RIFF/WAVE container."""
        data_size = len(samples) * 2
        header = (b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
                  + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
                  + b"data" + struct.pack("<I", data_size))
        return header + struct.pack(f"<{len(samples)}h", *samples)

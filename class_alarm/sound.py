"""Loops an alarm sound on the laptop until stopped. No third-party audio libraries needed."""

from __future__ import annotations

import logging
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

log = logging.getLogger(__name__)


def make_beep_wav(path: Path) -> Path:
    """Writes a loud two-tone beep pattern (about 2 seconds) to `path`."""
    rate = 22050
    pattern = [(880, 0.25), (0, 0.08), (660, 0.25), (0, 0.08)] * 3 + [(0, 0.35)]
    frames = bytearray()
    for freq, seconds in pattern:
        for i in range(int(rate * seconds)):
            sample = 0.0 if freq == 0 else math.sin(2 * math.pi * freq * i / rate)
            frames += struct.pack("<h", int(sample * 32767 * 0.9))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def _linux_player(path: Path) -> list[str] | None:
    for cmd in (
        ["paplay", str(path)],
        ["pw-play", str(path)],
        ["aplay", "-q", str(path)],
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)],
    ):
        if shutil.which(cmd[0]):
            return cmd
    return None


class AlarmSound:
    def __init__(self, sound_file: Path | None = None):
        if sound_file and sound_file.exists():
            self.path = sound_file
        else:
            if sound_file:
                log.warning("sound file %s not found, using built-in beep", sound_file)
            self.path = make_beep_wav(Path(tempfile.gettempdir()) / "class_alarm_beep.wav")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if sys.platform == "win32":
            import winsound

            flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP
            winsound.PlaySound(str(self.path), flags)
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _command(self) -> list[str] | None:
        if sys.platform == "darwin":
            return ["afplay", str(self.path)]
        return _linux_player(self.path)

    def _loop(self) -> None:
        cmd = self._command()
        if cmd is None:
            log.warning("no audio player found (install pulseaudio-utils or alsa-utils); using terminal bell")
        while not self._stop.is_set():
            if cmd is None:
                sys.stdout.write("\a")
                sys.stdout.flush()
                self._stop.wait(1)
                continue
            try:
                with self._lock:
                    if self._stop.is_set():
                        break
                    self._proc = subprocess.Popen(
                        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                self._proc.wait()
            except OSError as exc:
                log.warning("could not play sound with %s: %s", cmd[0], exc)
                cmd = None

    def stop(self) -> None:
        self._stop.set()
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(None, 0)
            return
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

"""The ringing loop: laptop sound + phone alerts until you dismiss, snooze, or the ring limit passes."""

from __future__ import annotations

import queue
import random
import sys
import threading
import time
from typing import Callable, Protocol

from class_alarm.config import AlarmConfig
from class_alarm.notifiers import Notifier


class Sound(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...


class LineReader:
    """Reads stdin lines on a background thread so the alarm can keep ringing while it waits."""

    def __init__(self, stream=None):
        self._stream = stream if stream is not None else sys.stdin
        self._queue: queue.Queue[str] = queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            while True:
                line = self._stream.readline()
                if not line:
                    break
                self._queue.put(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        self.closed.set()

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def get(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def ring(
    message: str,
    cfg: AlarmConfig,
    notifiers: list[Notifier],
    sound: Sound,
    reader: LineReader,
    clock: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
    poll_seconds: float = 1.0,
) -> str:
    """Returns 'dismissed' or 'timed_out'. Always leaves sound and phone alerts stopped."""
    try:
        return _ring(message, cfg, notifiers, sound, reader, clock, out, poll_seconds)
    except BaseException:  # Ctrl+C mid-alarm must not leave the phone repeating
        sound.stop()
        for n in notifiers:
            n.stop()
        raise


def _ring(message, cfg, notifiers, sound, reader, clock, out, poll_seconds) -> str:
    code = f"{random.randint(0, 9999):04d}" if cfg.dismiss_code else None
    deadline = clock() + cfg.ring_limit_minutes * 60
    snoozes_left = cfg.max_snoozes
    reader.drain()

    def is_dismiss(line: str) -> bool:
        text = line.strip()
        return text == code if code else text.lower() != "s"

    def silence() -> None:
        sound.stop()
        for n in notifiers:
            n.stop()

    if reader.closed.is_set():
        out("Warning: no keyboard input available, the alarm will stop only at the ring limit.")

    while True:
        out("")
        out(f"*** {message} ***")
        stop_hint = f"type {code} and press Enter" if code else "press Enter"
        snooze_hint = f", or type s to snooze {cfg.snooze_minutes} min ({snoozes_left} left)" if snoozes_left else ""
        out(f"To stop: {stop_hint}{snooze_hint}.")

        sound.start()
        for n in notifiers:
            n.start(message, clock())

        snoozed = False
        while not snoozed:
            if clock() >= deadline:
                silence()
                out("Ring limit reached, alarm stopped.")
                return "timed_out"
            line = reader.get(poll_seconds)
            for n in notifiers:
                n.tick(message, clock())
            if line is None:
                continue
            if line.strip().lower() == "s":
                if snoozes_left:
                    snoozes_left -= 1
                    snoozed = True
                else:
                    out(f"No snoozes left. To stop: {stop_hint}.")
            elif is_dismiss(line):
                silence()
                out("Alarm dismissed. Go to class!")
                return "dismissed"
            else:
                out(f"Wrong code. To stop: {stop_hint}.")

        silence()
        snooze_seconds = cfg.snooze_minutes * 60
        deadline += snooze_seconds
        wake_again = clock() + snooze_seconds
        out(f"Snoozed for {cfg.snooze_minutes} min. ({stop_hint} to cancel the alarm entirely)")
        while clock() < wake_again:
            line = reader.get(poll_seconds)
            if line is not None and line.strip().lower() != "s" and is_dismiss(line):
                out("Alarm dismissed. Go to class!")
                return "dismissed"

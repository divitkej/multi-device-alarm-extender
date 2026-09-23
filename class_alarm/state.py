"""Small on-disk state shared by the running alarm and one-off commands.

- state.json: shampoo date overrides and which phone events were already read
- events.jsonl: phone behavior log (one JSON object per line), used to learn sleep patterns
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

KEEP_EVENT_DAYS = 30
KEEP_SEEN_IDS = 2000


@dataclass
class State:
    shampoo_on: set[date] = field(default_factory=set)
    shampoo_off: set[date] = field(default_factory=set)
    seen_ids: list[str] = field(default_factory=list)

    def is_shampoo_override(self, day: date) -> bool | None:
        if day in self.shampoo_off:
            return False
        if day in self.shampoo_on:
            return True
        return None

    def set_shampoo(self, day: date, on: bool) -> None:
        (self.shampoo_on if on else self.shampoo_off).add(day)
        (self.shampoo_off if on else self.shampoo_on).discard(day)

    def prune(self, today: date) -> None:
        cutoff = today - timedelta(days=7)
        self.shampoo_on = {d for d in self.shampoo_on if d >= cutoff}
        self.shampoo_off = {d for d in self.shampoo_off if d >= cutoff}
        self.seen_ids = self.seen_ids[-KEEP_SEEN_IDS:]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StateStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"
        self._lock = threading.Lock()  # the web app and the alarm loop share one store

    def load(self) -> State:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        return State(
            shampoo_on={date.fromisoformat(d) for d in raw.get("shampoo_on", [])},
            shampoo_off={date.fromisoformat(d) for d in raw.get("shampoo_off", [])},
            seen_ids=list(raw.get("seen_ids", [])),
        )

    def save(self, state: State) -> None:
        state.prune(date.today())
        raw = {
            "shampoo_on": sorted(d.isoformat() for d in state.shampoo_on),
            "shampoo_off": sorted(d.isoformat() for d in state.shampoo_off),
            "seen_ids": state.seen_ids,
        }
        _atomic_write(self.path, json.dumps(raw, indent=2))

    def update(self, change) -> State:
        """Load, apply `change(state)`, save. Keeps concurrent writers from losing each other's edits."""
        with self._lock:
            state = self.load()
            change(state)
            self.save(state)
            return state


@dataclass(frozen=True)
class Event:
    ts: float  # unix seconds (from the ntfy server clock)
    kind: str
    id: str = ""


class EventLog:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "events.jsonl"
        self._lock = threading.Lock()

    def append(self, events: list[Event]) -> None:
        if not events:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps({"ts": e.ts, "kind": e.kind, "id": e.id}) + "\n")

    def read(self, since_ts: float = 0.0) -> list[Event]:
        events = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                raw = json.loads(line)
                event = Event(ts=float(raw["ts"]), kind=str(raw["kind"]), id=str(raw.get("id", "")))
            except (ValueError, KeyError, TypeError):
                continue  # a half-written line after a crash
            if event.ts >= since_ts:
                events.append(event)
        return sorted(events, key=lambda e: e.ts)

    def prune(self, now_ts: float) -> None:
        keep = self.read(now_ts - KEEP_EVENT_DAYS * 86400)
        text = "".join(json.dumps({"ts": e.ts, "kind": e.kind, "id": e.id}) + "\n" for e in keep)
        _atomic_write(self.path, text)

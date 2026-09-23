"""Reads a college timetable (weekly CSV or .ics calendar) into dated class sessions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from class_alarm.config import TimetableConfig


class TimetableError(Exception):
    pass


@dataclass(frozen=True)
class ClassSession:
    start: datetime  # timezone-aware, local time
    name: str
    location: str = ""


_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_DAY_GROUPS = {
    "weekdays": (0, 1, 2, 3, 4),
    "daily": (0, 1, 2, 3, 4, 5, 6),
    "everyday": (0, 1, 2, 3, 4, 5, 6),
}
_TIME_FORMATS = ("%H:%M", "%H.%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p")


def parse_days(text: str) -> tuple[int, ...]:
    """'Mon/Wed/Fri', 'Tue Thu', 'Monday', 'weekdays' -> weekday numbers (Mon=0)."""
    tokens = text.lower().replace(";", "/").replace("&", "/").replace("+", "/").replace(" ", "/")
    days: set[int] = set()
    for token in filter(None, (t.strip().rstrip(".") for t in tokens.split("/"))):
        if token in _DAY_GROUPS:
            days.update(_DAY_GROUPS[token])
        elif token in _DAY_NAMES:
            days.add(_DAY_NAMES[token])
        else:
            raise TimetableError(f"unknown day {token!r} (use Mon, Tue, Wed, Thu, Fri, Sat, Sun)")
    if not days:
        raise TimetableError("no day given")
    return tuple(sorted(days))


def parse_time(text: str) -> time:
    """'07:30', '7:30', '7:30 AM', '19:30' -> time."""
    cleaned = " ".join(text.strip().upper().split())
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    raise TimetableError(f"can't read time {text!r} (use 24h like 07:30 or 12h like 7:30 AM)")


@dataclass(frozen=True)
class WeeklyEntry:
    days: tuple[int, ...]
    start: time
    name: str
    location: str


def load_csv(path: Path) -> list[WeeklyEntry]:
    """CSV columns: day, start, [end], [course], [location]. Header row required."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TimetableError(f"can't read timetable {path}: {exc}") from exc

    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise TimetableError(f"{path} is empty")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}
    if "day" not in fields or "start" not in fields:
        raise TimetableError(f"{path} needs a header row with at least 'day' and 'start' columns")

    def col(row: dict, key: str) -> str:
        original = fields.get(key)
        return (row.get(original) or "").strip() if original else ""

    entries = []
    for line_no, row in enumerate(reader, start=2):
        day_text, start_text = col(row, "day"), col(row, "start")
        if not day_text and not start_text:
            continue  # blank line
        if day_text.startswith("#"):
            continue  # comment line
        try:
            entries.append(
                WeeklyEntry(
                    days=parse_days(day_text),
                    start=parse_time(start_text),
                    name=col(row, "course") or "Class",
                    location=col(row, "location"),
                )
            )
        except TimetableError as exc:
            raise TimetableError(f"{path} line {line_no}: {exc}") from None
    return entries


def _expand_weekly(entries: list[WeeklyEntry], first: date, last: date) -> list[ClassSession]:
    sessions = []
    day = first
    while day <= last:
        for entry in entries:
            if day.weekday() in entry.days:
                start = datetime.combine(day, entry.start).astimezone()
                sessions.append(ClassSession(start=start, name=entry.name, location=entry.location))
        day += timedelta(days=1)
    return sessions


def _load_ics(path: Path, first: date, last: date) -> list[ClassSession]:
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        raise TimetableError(
            "reading .ics timetables needs extra packages: pip install 'class-alarm[ics]'"
        ) from None
    try:
        calendar = icalendar.Calendar.from_ical(path.read_bytes())
    except OSError as exc:
        raise TimetableError(f"can't read timetable {path}: {exc}") from exc
    except ValueError as exc:
        raise TimetableError(f"{path} is not a valid .ics file: {exc}") from exc

    window_start = datetime.combine(first, time.min).astimezone()
    window_end = datetime.combine(last + timedelta(days=1), time.min).astimezone()
    sessions = []
    for event in recurring_ical_events.of(calendar).between(window_start, window_end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start = event.get("DTSTART").dt
        if not isinstance(start, datetime):
            continue  # all-day events (holidays, deadlines) are not classes
        # Floating times (no timezone) mean "local time"; astimezone() handles both cases.
        start = start.astimezone()
        sessions.append(
            ClassSession(
                start=start,
                name=str(event.get("SUMMARY", "Class")).strip() or "Class",
                location=str(event.get("LOCATION", "")).strip(),
            )
        )
    return sessions


def sessions_between(cfg: TimetableConfig, first: date, last: date) -> list[ClassSession]:
    """All classes from `first` to `last` inclusive, after term, holiday and keyword filters."""
    path = cfg.path
    if not path.exists():
        raise TimetableError(f"timetable file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        sessions = _expand_weekly(load_csv(path), first, last)
    elif suffix in (".ics", ".ical"):
        sessions = _load_ics(path, first, last)
    else:
        raise TimetableError(f"unsupported timetable type {suffix!r}: use .csv or .ics")

    def keep(s: ClassSession) -> bool:
        d = s.start.date()
        if not first <= d <= last:
            return False
        if cfg.term_start and d < cfg.term_start:
            return False
        if cfg.term_end and d > cfg.term_end:
            return False
        if d in cfg.skip_dates:
            return False
        name = s.name.lower()
        return not any(k in name for k in cfg.ignore_keywords)

    return sorted(filter(keep, sessions), key=lambda s: s.start)

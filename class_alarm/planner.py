"""Turns class sessions into wake-up alarms: one per day, lead_minutes before the first class."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from class_alarm.config import TimetableConfig
from class_alarm.timetable import ClassSession, sessions_between


@dataclass(frozen=True)
class Alarm:
    wake_at: datetime
    first_class: ClassSession

    @property
    def key(self) -> str:
        return f"{self.wake_at.isoformat()}|{self.first_class.name}"

    def message(self) -> str:
        c = self.first_class
        where = f" in {c.location}" if c.location else ""
        return f"Wake up! {c.name} starts at {c.start:%H:%M}{where}."


def plan_alarms(sessions: list[ClassSession], lead_minutes: int) -> list[Alarm]:
    """Earliest class of each day -> alarm at (start - lead_minutes)."""
    first_by_day: dict = {}
    for s in sessions:
        day = s.start.date()
        if day not in first_by_day or s.start < first_by_day[day].start:
            first_by_day[day] = s
    lead = timedelta(minutes=lead_minutes)
    return [Alarm(wake_at=s.start - lead, first_class=s) for _, s in sorted(first_by_day.items())]


def upcoming_alarms(cfg: TimetableConfig, now: datetime, days: int = 8) -> list[Alarm]:
    """Alarms whose class has not started yet, looking `days` ahead from `now`."""
    sessions = sessions_between(cfg, now.date(), now.date() + timedelta(days=days))
    return [a for a in plan_alarms(sessions, cfg.lead_minutes) if a.first_class.start > now]

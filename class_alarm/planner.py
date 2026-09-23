"""Turns class sessions into wake-up alarms: one per day, lead_minutes before the first class."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from class_alarm.config import ShampooConfig, TimetableConfig
from class_alarm.state import State
from class_alarm.timetable import ClassSession, sessions_between

LeadFor = Callable[[date], tuple[int, bool]]  # class date -> (lead minutes, is shampoo day)


@dataclass(frozen=True)
class Alarm:
    wake_at: datetime
    first_class: ClassSession
    shampoo: bool = False

    @property
    def key(self) -> str:
        # Keyed by the class, not the wake time, so moving the wake time (e.g. marking a
        # shampoo day after the alarm already rang) never rings the same morning twice.
        return f"{self.first_class.start.isoformat()}|{self.first_class.name}"

    def message(self) -> str:
        c = self.first_class
        where = f" in {c.location}" if c.location else ""
        extra = " Shampoo day." if self.shampoo else ""
        return f"Wake up!{extra} {c.name} starts at {c.start:%H:%M}{where}."


def lead_function(lead_minutes: int, shampoo: ShampooConfig | None = None, state: State | None = None) -> LeadFor:
    """Normal lead, or the longer shampoo lead on shampoo days (weekly days or one-off overrides)."""

    def lead_for(day: date) -> tuple[int, bool]:
        is_shampoo = bool(shampoo and day.weekday() in shampoo.days)
        override = state.is_shampoo_override(day) if state else None
        if override is not None:
            is_shampoo = override
        if is_shampoo and shampoo:
            return shampoo.lead_minutes, True
        return lead_minutes, False

    return lead_for


def plan_alarms(sessions: list[ClassSession], lead: int | LeadFor) -> list[Alarm]:
    """Earliest class of each day -> alarm at (start - lead)."""
    lead_for = lead if callable(lead) else lead_function(lead)
    first_by_day: dict[date, ClassSession] = {}
    for s in sessions:
        day = s.start.date()
        if day not in first_by_day or s.start < first_by_day[day].start:
            first_by_day[day] = s
    alarms = []
    for day, s in sorted(first_by_day.items()):
        minutes, is_shampoo = lead_for(day)
        alarms.append(Alarm(wake_at=s.start - timedelta(minutes=minutes), first_class=s, shampoo=is_shampoo))
    return alarms


def upcoming_alarms(
    cfg: TimetableConfig, now: datetime, days: int = 8, lead: int | LeadFor | None = None
) -> list[Alarm]:
    """Alarms whose class has not started yet, looking `days` ahead from `now`."""
    sessions = sessions_between(cfg, now.date(), now.date() + timedelta(days=days))
    alarms = plan_alarms(sessions, cfg.lead_minutes if lead is None else lead)
    return [a for a in alarms if a.first_class.start > now]

"""Phone behavior: collecting events from the phone and turning them into sleep estimates.

The phone reports events (app opened, charger plugged in, Sleep Focus on, ...) by publishing a
short word to a private ntfy topic. iOS Shortcuts automations or Android automation apps do this.
The laptop polls that topic, logs the events, and learns when you usually go quiet at night.
"""

from __future__ import annotations

import json
import logging
import statistics
import time as _time
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from class_alarm.config import BehaviorConfig, SleepConfig
from class_alarm.notifiers import _describe_error, get
from class_alarm.state import Event, EventLog, StateStore

log = logging.getLogger(__name__)

# Things you do on the phone. Any of these means you were using it at that moment.
ACTIVITY = {
    "app_open", "app_close", "unlock", "charger_on", "charger_off",
    "sleep_focus_on", "sleep_focus_off", "awake", "shampoo", "no_shampoo",
}
# Plugging in the charger or turning on Sleep Focus happen when going TO bed, so they
# don't prove you're up. Everything else after the alarm does.
AWAKE_SIGNALS = ACTIVITY - {"charger_on", "sleep_focus_on"}
COMMANDS = {"shampoo", "no_shampoo"}

NIGHT_START = time(18, 0)  # a "night" runs from 18:00 to noon the next day
NIGHT_END = time(12, 0)
MIN_SLEEP_GAP = timedelta(hours=3)
HISTORY_NIGHTS = 14
MIN_NIGHTS_FOR_PATTERN = 3


class EventFeed:
    """Pulls new phone events from the ntfy events topic into the local log."""

    def __init__(self, cfg: BehaviorConfig, store: StateStore, event_log: EventLog, getter=get):
        self.cfg = cfg
        self.store = store
        self.log = event_log
        self._get = getter

    def _url(self) -> str:
        topic = urllib.parse.quote(self.cfg.events_topic, safe="")
        # ntfy.sh keeps messages for 12 hours, so a laptop that slept overnight still gets them.
        # Ask only for what's newer than the last logged event; seen ids drop the overlap.
        logged = self.log.read(_time.time() - 12 * 3600)
        since = str(int(logged[-1].ts)) if logged else "12h"
        return f"{self.cfg.server}/{topic}/json?poll=1&since={since}"

    def poll(self) -> list[Event]:
        headers = {"Authorization": f"Bearer {self.cfg.token}"} if self.cfg.token else {}
        try:
            body = self._get(self._url(), headers)
        except Exception as exc:
            log.warning("behavior: could not read phone events: %s", _describe_error(exc))
            return []

        seen = set(self.store.load().seen_ids)
        new: list[Event] = []
        new_ids: list[str] = []
        for line in body.decode("utf-8", "replace").splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            msg_id = msg.get("id")
            if msg.get("event") != "message" or not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            new_ids.append(msg_id)
            kind = str(msg.get("message", "")).strip().lower()
            if kind not in ACTIVITY:
                log.warning("behavior: ignoring unknown phone event %r", kind)
                continue
            new.append(Event(ts=float(msg.get("time", 0)), kind=kind, id=msg_id))

        if new:
            new.sort(key=lambda e: e.ts)
            self.log.append(new)
        if new_ids:
            self.store.update(lambda s: s.seen_ids.extend(i for i in new_ids if i not in s.seen_ids))
        return new


# --- sleep analysis ------------------------------------------------------------

def _local(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def sleep_period(events: list[Event], night: date) -> tuple[datetime, datetime] | None:
    """(fell quiet, first use next morning) for the night starting on `night`, or None.

    The longest stretch with no phone activity between 18:00 and noon, if at least 3 hours.
    It must be closed by a morning event, otherwise the data may just be missing.
    """
    start = datetime.combine(night, NIGHT_START).astimezone()
    end = datetime.combine(night + timedelta(days=1), NIGHT_END).astimezone()
    times = sorted(_local(e.ts) for e in events if e.kind in ACTIVITY and start <= _local(e.ts) <= end)
    best: tuple[datetime, datetime] | None = None
    for a, b in zip(times, times[1:]):
        if b - a >= MIN_SLEEP_GAP and (best is None or b - a > best[1] - best[0]):
            best = (a, b)
    return best


@dataclass(frozen=True)
class SleepPattern:
    usual_quiet: time  # when you usually stop using the phone
    usual_hours: float  # average hours between going quiet and first use in the morning
    nights: int


def _minutes_into_night(moment: datetime) -> float:
    night = moment.date() if moment.time() >= NIGHT_START else moment.date() - timedelta(days=1)
    return (moment - datetime.combine(night, NIGHT_START).astimezone()).total_seconds() / 60


def learn_pattern(events: list[Event], today: date, nights: int = HISTORY_NIGHTS) -> SleepPattern | None:
    periods = [
        p for p in (sleep_period(events, today - timedelta(days=i)) for i in range(1, nights + 1)) if p
    ]
    if len(periods) < MIN_NIGHTS_FOR_PATTERN:
        return None
    # Minutes after 18:00 so that 23:30 and 01:30 average correctly across midnight.
    median = statistics.median(_minutes_into_night(quiet) for quiet, _ in periods)
    usual = (datetime.combine(date(2000, 1, 1), NIGHT_START) + timedelta(minutes=median)).time()
    hours = statistics.mean((b - a).total_seconds() / 3600 for a, b in periods)
    return SleepPattern(usual_quiet=usual.replace(second=0, microsecond=0), usual_hours=hours, nights=len(periods))


def fmt_duration(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_clock(moment: datetime | time) -> str:
    return moment.strftime("%I:%M %p").lstrip("0")


def projected_quiet(pattern: SleepPattern, wake_at: datetime) -> datetime:
    """When you'd go quiet before `wake_at` if tonight follows your usual pattern."""
    night = (wake_at - timedelta(hours=18)).date()
    at = datetime.combine(night, pattern.usual_quiet).astimezone()
    if pattern.usual_quiet < NIGHT_START:
        at += timedelta(days=1)
    return at


class SleepCoach:
    """Decides which bedtime messages are due. Pure logic: the caller sends what it returns."""

    BEDTIME_GRACE = timedelta(minutes=15)

    def __init__(self, cfg: SleepConfig):
        self.cfg = cfg
        self.sent: set[str] = set()
        self.last_reminder: dict[str, datetime] = {}
        self.nags: dict[str, int] = {}

    def bedtime(self, wake_at: datetime) -> datetime:
        return wake_at - timedelta(hours=self.cfg.sleep_hours, minutes=self.cfg.fall_asleep_minutes)

    def due(
        self,
        now: datetime,
        wake_at: datetime,
        alarm_key: str,
        recent_activity: list[datetime],
        pattern: SleepPattern | None,
    ) -> list[str]:
        bed = self.bedtime(wake_at)
        if now >= wake_at or wake_at - now > timedelta(hours=20):
            return []
        wake_txt = f"your {fmt_clock(wake_at)} alarm"
        messages = []

        wind_key = f"{alarm_key}|wind"
        wind_at = bed - timedelta(minutes=self.cfg.wind_down_minutes)
        if self.cfg.wind_down_minutes and wind_at <= now < bed and wind_key not in self.sent:
            self.sent.add(wind_key)
            text = f"Bedtime is {fmt_clock(bed)} for {wake_txt}. Start winding down."
            if pattern:
                quiet = projected_quiet(pattern, wake_at)
                if quiet > bed:
                    text += (
                        f" You usually put your phone down around {fmt_clock(pattern.usual_quiet)},"
                        f" which would leave only {fmt_duration(wake_at - quiet)}."
                    )
            messages.append(text)

        bed_key = f"{alarm_key}|bed"
        if bed <= now < bed + self.BEDTIME_GRACE and bed_key not in self.sent:
            self.sent.add(bed_key)
            self.last_reminder[alarm_key] = now
            messages.append(
                f"Time to sleep. Sleeping now gives you {fmt_duration(wake_at - now - timedelta(minutes=self.cfg.fall_asleep_minutes))} before {wake_txt}."
            )

        last = self.last_reminder.get(alarm_key, bed)
        if (
            now >= bed
            and self.nags.get(alarm_key, 0) < self.cfg.max_nags
            and now - last >= timedelta(minutes=self.cfg.nag_minutes)
            and any(t > last for t in recent_activity)
        ):
            self.nags[alarm_key] = self.nags.get(alarm_key, 0) + 1
            self.last_reminder[alarm_key] = now
            left = wake_at - now - timedelta(minutes=self.cfg.fall_asleep_minutes)
            messages.append(f"Still on your phone. If you sleep now you get {fmt_duration(left)} before {wake_txt}.")
        return messages


def awake_signal_since(events: list[Event], since_ts: float) -> Event | None:
    for e in events:
        if e.kind in AWAKE_SIGNALS and e.ts >= since_ts:
            return e
    return None


def recent_activity_times(events: list[Event]) -> list[datetime]:
    return [_local(e.ts) for e in events if e.kind in ACTIVITY]


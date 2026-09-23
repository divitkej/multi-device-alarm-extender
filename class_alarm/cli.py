"""Command line: plan, run, test, test-phone, wake-next, shampoo, events, sleep-report."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from class_alarm.awake import confirm_awake
from class_alarm.behavior import (
    COMMANDS,
    EventFeed,
    SleepCoach,
    SleepPattern,
    fmt_clock,
    fmt_duration,
    learn_pattern,
    projected_quiet,
    recent_activity_times,
    sleep_period,
)
from class_alarm.config import Config, ConfigError, load_config
from class_alarm.notifiers import Messenger, build_notifiers
from class_alarm.planner import Alarm, lead_function, upcoming_alarms
from class_alarm.ringer import LineReader, ring
from class_alarm.sound import AlarmSound
from class_alarm.state import Event, EventLog, StateStore
from class_alarm.timetable import TimetableError
from class_alarm.wake import WakeError, schedule_wake

log = logging.getLogger("class_alarm")

WAKE_EARLY = timedelta(minutes=2)  # wake the machine a little before the alarm so it is ready
POLL_SECONDS = 15  # short sleeps so a laptop that just woke up notices the time quickly
EVENT_POLL_SECONDS = 30


def _describe(alarm: Alarm) -> str:
    c = alarm.first_class
    where = f", {c.location}" if c.location else ""
    shampoo = "  (shampoo day)" if alarm.shampoo else ""
    return f"{alarm.wake_at:%a %d %b  %H:%M}  wake  ->  {c.start:%H:%M}  {c.name}{where}{shampoo}"


class App:
    """Everything the commands share: config, saved state, phone event feed."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg: Config = load_config(config_path)
        self.store = StateStore(self.cfg.data_dir)
        self.events = EventLog(self.cfg.data_dir)
        self._pattern: tuple[date, SleepPattern | None] | None = None

    def reload(self) -> None:
        self.cfg = load_config(self.config_path)

    def alarms(self, now: datetime, days: int = 8) -> list[Alarm]:
        lead = lead_function(self.cfg.timetable.lead_minutes, self.cfg.shampoo, self.store.load())
        return upcoming_alarms(self.cfg.timetable, now, days, lead)

    def messenger(self) -> Messenger:
        return Messenger(self.cfg.phone, self.cfg.behavior)

    def poll_events(self) -> list[Event]:
        if not self.cfg.behavior.enabled:
            return []
        new = EventFeed(self.cfg.behavior, self.store, self.events).poll()
        for event in new:
            if event.kind in COMMANDS:
                self._handle_command(event)
        return new

    def _handle_command(self, event: Event) -> None:
        """'shampoo' / 'no_shampoo' from the phone apply to the next morning with class."""
        at = datetime.fromtimestamp(event.ts).astimezone()
        try:
            upcoming = self.alarms(at)
        except TimetableError as exc:
            log.error("can't apply %s: %s", event.kind, exc)
            return
        if not upcoming:
            self.messenger().info("Class Alarm", "No upcoming class to change.")
            return
        day = upcoming[0].first_class.start.date()
        on = event.kind == "shampoo"
        self.store.update(lambda s: s.set_shampoo(day, on))
        alarm = next((a for a in self.alarms(at) if a.first_class.start.date() == day), None)
        if alarm is None:
            return
        label = "Shampoo day" if on else "No shampoo"
        log.info("%s for %s from phone, alarm now %s", label, day, f"{alarm.wake_at:%H:%M}")
        self.messenger().info(
            "Class Alarm", f"{label} on {day:%a}: alarm set for {fmt_clock(alarm.wake_at)} ({alarm.first_class.name} at {fmt_clock(alarm.first_class.start)})."
        )

    def pattern(self, today: date) -> SleepPattern | None:
        if self._pattern is None or self._pattern[0] != today:
            events = self.events.read(time.time() - 16 * 86400)
            self._pattern = (today, learn_pattern(events, today))
        return self._pattern[1]


# --- commands ------------------------------------------------------------------

def cmd_plan(app: App, days: int) -> int:
    now = datetime.now().astimezone()
    alarms = app.alarms(now, days)
    if not alarms:
        print(f"No classes in the next {days} days, so no alarms.")
        return 0
    cfg = app.cfg
    shampoo = f", {cfg.shampoo.lead_minutes} min on shampoo days" if cfg.shampoo.days or app.store.load().shampoo_on else ""
    print(f"Alarms ({cfg.timetable.lead_minutes} min before your first class each day{shampoo}):")
    for a in alarms:
        late = "  (already past wake time)" if a.wake_at <= now else ""
        print(f"  {_describe(a)}{late}")
    return 0


def _ring_alarm(cfg: Config, message: str, reader: LineReader, phone: bool = True) -> str:
    notifiers = build_notifiers(cfg.phone) if phone else []
    if phone and not notifiers:
        log.warning("no phone channel is enabled in config, only the laptop will ring")
    return ring(message, cfg.alarm, notifiers, AlarmSound(cfg.alarm.sound_file), reader)


def _try_schedule_wake(when: datetime) -> bool:
    try:
        schedule_wake(when)
    except WakeError as exc:
        log.warning("could not schedule laptop wake-up for %s: %s", f"{when:%a %H:%M}", exc)
        return False
    log.info("laptop will wake from sleep at %s", f"{when:%a %d %b %H:%M}")
    return True


def _ring_until_awake(app: App, alarm: Alarm, reader: LineReader) -> None:
    outcome = _ring_alarm(app.cfg, alarm.message(), reader)
    log.info("alarm for %s %s", alarm.first_class.name, outcome.replace("_", " "))
    check = app.cfg.awake_check
    if outcome != "dismissed" or not check.enabled:
        return
    for _ in range(check.max_rerings):
        if confirm_awake(check, app.messenger(), app.poll_events):
            print("Confirmed awake. Have a good day.")
            return
        if datetime.now().astimezone() >= alarm.first_class.start:
            return
        print("No answer from your phone. Ringing again.")
        outcome = _ring_alarm(app.cfg, "You did not confirm you're awake. " + alarm.message(), reader)
        if outcome != "dismissed":
            return


def _sleep_messages(app: App, coach: SleepCoach, now: datetime, nxt: Alarm) -> None:
    recent = recent_activity_times(app.events.read(now.timestamp() - 6 * 3600))
    for text in coach.due(now, nxt.wake_at, nxt.key, recent, app.pattern(now.date())):
        log.info("sleep reminder: %s", text)
        app.messenger().info("Bedtime", text)


def cmd_run(config_path: Path) -> int:
    app = App(config_path)
    app.events.prune(time.time())
    alarms = app.alarms(datetime.now().astimezone())
    reader = LineReader()
    coach = SleepCoach(app.cfg.sleep)
    fired: set[str] = set()
    announced: str | None = None
    wake_scheduled_for: datetime | None = None
    last_event_poll = 0.0
    print("Class Alarm is running. Keep this window open and the laptop plugged in. Ctrl+C to quit.")

    while True:
        try:
            app.reload()
        except ConfigError as exc:
            log.error("%s (still using the last good config)", exc)
        coach.cfg = app.cfg.sleep

        if time.monotonic() - last_event_poll >= EVENT_POLL_SECONDS:
            last_event_poll = time.monotonic()
            app.poll_events()

        now = datetime.now().astimezone()
        try:
            alarms = app.alarms(now)
        except TimetableError as exc:
            log.error("%s (still using the last good timetable)", exc)

        pending = [a for a in alarms if a.key not in fired and a.first_class.start > now]
        if not pending:
            if announced != "none":
                print("No upcoming classes in the next week. Waiting for timetable changes.")
                announced = "none"
            time.sleep(POLL_SECONDS)
            continue

        nxt = pending[0]
        if announced != nxt.key + nxt.wake_at.isoformat():
            print(f"Next alarm: {_describe(nxt)}")
            announced = nxt.key + nxt.wake_at.isoformat()

        if app.cfg.sleep.enabled:
            _sleep_messages(app, coach, now, nxt)

        if app.cfg.alarm.os_wake:
            targets = [nxt.wake_at - WAKE_EARLY]
            if app.cfg.sleep.enabled:
                wind = coach.bedtime(nxt.wake_at) - timedelta(minutes=app.cfg.sleep.wind_down_minutes)
                targets.append(wind - WAKE_EARLY)
            future = [t for t in targets if t > now]
            target = min(future) if future else None
            if target and target != wake_scheduled_for and _try_schedule_wake(target):
                wake_scheduled_for = target

        if nxt.wake_at <= now:
            fired.add(nxt.key)
            _ring_until_awake(app, nxt, reader)
            continue

        time.sleep(min(POLL_SECONDS, max(1.0, (nxt.wake_at - now).total_seconds())))


def cmd_test(app: App, phone: bool) -> int:
    outcome = _ring_alarm(app.cfg, "Test alarm from Class Alarm.", LineReader(), phone)
    return 0 if outcome == "dismissed" else 1


def cmd_test_phone(app: App) -> int:
    notifiers = build_notifiers(app.cfg.phone)
    if not notifiers:
        print("No phone channel is enabled. Turn one on under [phone.*] in config.toml.")
        return 1
    for n in notifiers:
        print(f"Sending test alert via {n.name}...")
        n.start("Test alert from Class Alarm. If you see this, your phone is set up.")
        n.stop()
    print("Done. Check your phone. Any failures are logged above.")
    return 0


def cmd_wake_next(app: App) -> int:
    now = datetime.now().astimezone()
    for a in app.alarms(now):
        target = a.wake_at - WAKE_EARLY
        if target > now:
            return 0 if _try_schedule_wake(target) else 1
    print("No upcoming alarm to schedule a wake-up for.")
    return 1


def _parse_day(text: str | None, app: App) -> date | None:
    today = date.today()
    if text in (None, "next"):
        upcoming = app.alarms(datetime.now().astimezone())
        return upcoming[0].first_class.start.date() if upcoming else None
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"can't read date {text!r} (use next, today, tomorrow or 2026-09-24)") from None


def cmd_shampoo(app: App, when: str | None, off: bool) -> int:
    day = _parse_day(when, app)
    if day is None:
        print("No upcoming class, nothing to change.")
        return 1
    app.store.update(lambda s: s.set_shampoo(day, not off))
    print(f"{'No shampoo' if off else 'Shampoo day'} set for {day:%a %d %b}.")
    now = datetime.now().astimezone()
    for a in app.alarms(now):
        if a.first_class.start.date() == day:
            print(f"  {_describe(a)}")
            break
    else:
        print("  (no class that day, so no alarm)")
    return 0


def cmd_events(app: App) -> int:
    if not app.cfg.behavior.enabled:
        print("Phone behavior tracking is off. Set [behavior] enabled = true in config.toml.")
        return 1
    new = app.poll_events()
    print(f"Fetched {len(new)} new event(s) from your phone.")
    recent = app.events.read(time.time() - 86400)
    if not recent:
        print("No phone events in the last 24 hours. Check your Shortcuts automations.")
        return 0
    print("Last 24 hours:")
    for e in recent[-30:]:
        print(f"  {datetime.fromtimestamp(e.ts):%a %H:%M:%S}  {e.kind}")
    return 0


def cmd_sleep_report(app: App) -> int:
    app.poll_events()
    today = date.today()
    events = app.events.read(time.time() - 16 * 86400)
    print("Recent nights (phone quiet -> first use):")
    any_night = False
    for i in range(7, 0, -1):
        night = today - timedelta(days=i)
        period = sleep_period(events, night)
        if period:
            any_night = True
            quiet, first = period
            print(f"  {night:%a %d %b}  {fmt_clock(quiet):>8} -> {fmt_clock(first):>8}  {fmt_duration(first - quiet)}")
    if not any_night:
        print("  Not enough phone data yet. It needs a few nights of events.")
    pattern = learn_pattern(events, today)
    if pattern:
        print(f"Usual: phone down around {fmt_clock(pattern.usual_quiet)}, {pattern.usual_hours:.1f}h until you pick it up ({pattern.nights} nights).")
    now = datetime.now().astimezone()
    upcoming = [a for a in app.alarms(now) if a.wake_at > now]
    if upcoming:
        nxt = upcoming[0]
        bed = SleepCoach(app.cfg.sleep).bedtime(nxt.wake_at)
        print(f"Next alarm {fmt_clock(nxt.wake_at)} {nxt.wake_at:%a}: go to bed by {fmt_clock(bed)} for {app.cfg.sleep.sleep_hours:g}h of sleep.")
        if pattern:
            quiet = projected_quiet(pattern, nxt.wake_at)
            print(f"On your usual pattern you'd put the phone down at {fmt_clock(quiet)} and get about {fmt_duration(nxt.wake_at - quiet)}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="class-alarm",
        description="Wakes you before your first class, on your laptop and your phone.",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config.toml"), help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="show the upcoming wake-up times")
    p_plan.add_argument("--days", type=int, default=7)
    sub.add_parser("run", help="run the alarm clock (leave this running)")
    p_test = sub.add_parser("test", help="ring right now to test sound, phone and dismissing")
    p_test.add_argument("--no-phone", action="store_true", help="laptop only")
    sub.add_parser("test-phone", help="send one test alert to each enabled phone channel")
    sub.add_parser("wake-next", help="tell the OS to wake the laptop before the next alarm")
    p_sh = sub.add_parser("shampoo", help="mark a shampoo day (earlier alarm)")
    p_sh.add_argument("when", nargs="?", help="next (default), today, tomorrow, or a date like 2026-09-24")
    p_sh.add_argument("--off", action="store_true", help="not a shampoo day, even if it's a weekly one")
    sub.add_parser("events", help="show phone behavior events received recently")
    sub.add_parser("sleep-report", help="show your sleep pattern and tonight's bedtime")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    try:
        if args.command == "run":
            return cmd_run(args.config)
        app = App(args.config)
        if args.command == "plan":
            return cmd_plan(app, max(1, args.days))
        if args.command == "test":
            return cmd_test(app, phone=not args.no_phone)
        if args.command == "test-phone":
            return cmd_test_phone(app)
        if args.command == "wake-next":
            return cmd_wake_next(app)
        if args.command == "shampoo":
            return cmd_shampoo(app, args.when, args.off)
        if args.command == "events":
            return cmd_events(app)
        if args.command == "sleep-report":
            return cmd_sleep_report(app)
    except (ConfigError, TimetableError) as exc:
        print(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    return 0

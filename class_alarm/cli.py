"""Command line: plan, run, test, test-phone, wake-next."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from class_alarm.config import Config, ConfigError, load_config
from class_alarm.notifiers import build_notifiers
from class_alarm.planner import Alarm, upcoming_alarms
from class_alarm.ringer import LineReader, ring
from class_alarm.sound import AlarmSound
from class_alarm.timetable import TimetableError
from class_alarm.wake import WakeError, schedule_wake

log = logging.getLogger("class_alarm")

WAKE_EARLY = timedelta(minutes=2)  # wake the machine a little before the alarm so it is ready
POLL_SECONDS = 15  # short sleeps so a laptop that just woke up notices the time quickly


def _describe(alarm: Alarm) -> str:
    c = alarm.first_class
    where = f", {c.location}" if c.location else ""
    return f"{alarm.wake_at:%a %d %b  %H:%M}  wake  ->  {c.start:%H:%M}  {c.name}{where}"


def cmd_plan(cfg: Config, days: int) -> int:
    now = datetime.now().astimezone()
    alarms = upcoming_alarms(cfg.timetable, now, days)
    if not alarms:
        print(f"No classes in the next {days} days, so no alarms.")
        return 0
    print(f"Alarms ({cfg.timetable.lead_minutes} min before your first class each day):")
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


def cmd_run(config_path: Path) -> int:
    cfg = load_config(config_path)
    alarms = upcoming_alarms(cfg.timetable, datetime.now().astimezone())
    reader = LineReader()
    fired: set[str] = set()
    announced: str | None = None
    wake_scheduled_for: datetime | None = None
    print("Class Alarm is running. Keep this window open and the laptop plugged in. Ctrl+C to quit.")

    while True:
        now = datetime.now().astimezone()
        try:
            cfg = load_config(config_path)
            alarms = upcoming_alarms(cfg.timetable, now)
        except (ConfigError, TimetableError) as exc:
            log.error("%s (still using the last good timetable)", exc)

        pending = [a for a in alarms if a.key not in fired and a.first_class.start > now]
        if not pending:
            if announced != "none":
                print("No upcoming classes in the next week. Waiting for timetable changes.")
                announced = "none"
            time.sleep(60)
            continue

        nxt = pending[0]
        if announced != nxt.key:
            print(f"Next alarm: {_describe(nxt)}")
            announced = nxt.key

        if cfg.alarm.os_wake:
            target = nxt.wake_at - WAKE_EARLY
            if target > now and target != wake_scheduled_for and _try_schedule_wake(target):
                wake_scheduled_for = target

        if nxt.wake_at <= now:
            fired.add(nxt.key)
            outcome = _ring_alarm(cfg, nxt.message(), reader)
            log.info("alarm for %s %s", nxt.first_class.name, outcome.replace("_", " "))
            continue

        time.sleep(min(POLL_SECONDS, max(1.0, (nxt.wake_at - now).total_seconds())))


def cmd_test(cfg: Config, phone: bool) -> int:
    outcome = _ring_alarm(cfg, "Test alarm from Class Alarm.", LineReader(), phone)
    return 0 if outcome == "dismissed" else 1


def cmd_test_phone(cfg: Config) -> int:
    notifiers = build_notifiers(cfg.phone)
    if not notifiers:
        print("No phone channel is enabled. Turn one on under [phone.*] in config.toml.")
        return 1
    for n in notifiers:
        print(f"Sending test alert via {n.name}...")
        n.start("Test alert from Class Alarm. If you see this, your phone is set up.")
        n.stop()
    print("Done. Check your phone. Any failures are logged above.")
    return 0


def cmd_wake_next(cfg: Config) -> int:
    now = datetime.now().astimezone()
    for a in upcoming_alarms(cfg.timetable, now):
        target = a.wake_at - WAKE_EARLY
        if target > now:
            return 0 if _try_schedule_wake(target) else 1
    print("No upcoming alarm to schedule a wake-up for.")
    return 1


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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    try:
        if args.command == "run":
            return cmd_run(args.config)
        cfg = load_config(args.config)
        if args.command == "plan":
            return cmd_plan(cfg, max(1, args.days))
        if args.command == "test":
            return cmd_test(cfg, phone=not args.no_phone)
        if args.command == "test-phone":
            return cmd_test_phone(cfg)
        if args.command == "wake-next":
            return cmd_wake_next(cfg)
    except (ConfigError, TimetableError) as exc:
        print(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    return 0

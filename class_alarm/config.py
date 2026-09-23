"""Loads and validates config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class TimetableConfig:
    path: Path
    lead_minutes: int = 45
    term_start: date | None = None
    term_end: date | None = None
    skip_dates: frozenset[date] = frozenset()
    ignore_keywords: tuple[str, ...] = ()


@dataclass
class AlarmConfig:
    snooze_minutes: int = 5
    max_snoozes: int = 2
    ring_limit_minutes: int = 30
    dismiss_code: bool = True
    sound_file: Path | None = None
    os_wake: bool = False


@dataclass
class NtfyConfig:
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""


@dataclass
class PushoverConfig:
    enabled: bool = False
    app_token: str = ""
    user_key: str = ""
    sound: str = "persistent"
    retry_seconds: int = 30


@dataclass
class TwilioConfig:
    enabled: bool = False
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    to_number: str = ""
    call_every_minutes: int = 3


@dataclass
class PhoneConfig:
    repeat_seconds: int = 60
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    pushover: PushoverConfig = field(default_factory=PushoverConfig)
    twilio: TwilioConfig = field(default_factory=TwilioConfig)


@dataclass
class ShampooConfig:
    days: tuple[int, ...] = ()  # weekdays, Mon=0
    lead_minutes: int = 75


@dataclass
class BehaviorConfig:
    enabled: bool = False
    server: str = "https://ntfy.sh"
    events_topic: str = ""
    token: str = ""


@dataclass
class SleepConfig:
    enabled: bool = False
    sleep_hours: float = 7.5
    fall_asleep_minutes: int = 15
    wind_down_minutes: int = 30
    nag_minutes: int = 30
    max_nags: int = 4


@dataclass
class AwakeCheckConfig:
    enabled: bool = False
    check_after_minutes: int = 5
    confirm_window_minutes: int = 5
    max_rerings: int = 2


@dataclass
class Config:
    timetable: TimetableConfig
    alarm: AlarmConfig
    phone: PhoneConfig
    shampoo: ShampooConfig = field(default_factory=ShampooConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    awake_check: AwakeCheckConfig = field(default_factory=AwakeCheckConfig)
    data_dir: Path = Path("data")


def _date(value: object, where: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"{where}: expected a date like 2026-09-01, got {value!r}")


def _int(section: dict, key: str, default: int, minimum: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where}.{key}: expected a whole number >= {minimum}, got {value!r}")
    return value


def _float(section: dict, key: str, default: float, low: float, high: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ConfigError(f"{where}.{key}: expected a number from {low} to {high}, got {value!r}")
    return float(value)


def _bool(section: dict, key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected true or false, got {value!r}")
    return value


def _str(section: dict, key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: expected text, got {value!r}")
    return value.strip()


def _require(enabled: bool, values: dict[str, str], where: str) -> None:
    if not enabled:
        return
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise ConfigError(f"{where} is enabled but missing: {', '.join(missing)}")


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.toml to config.toml and edit it."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    base = path.resolve().parent

    tt = raw.get("timetable", {})
    tt_path = _str(tt, "path", "", "timetable")
    if not tt_path:
        raise ConfigError("timetable.path is required (a .csv or .ics file)")
    timetable_path = Path(tt_path).expanduser()
    if not timetable_path.is_absolute():
        timetable_path = base / timetable_path

    term_start = _date(tt["term_start"], "timetable.term_start") if tt.get("term_start") else None
    term_end = _date(tt["term_end"], "timetable.term_end") if tt.get("term_end") else None
    if term_start and term_end and term_end < term_start:
        raise ConfigError("timetable.term_end is before timetable.term_start")

    skip_raw = tt.get("skip_dates", [])
    if not isinstance(skip_raw, list):
        raise ConfigError("timetable.skip_dates must be a list of dates")
    ignore_raw = tt.get("ignore_keywords", [])
    if not isinstance(ignore_raw, list) or not all(isinstance(k, str) for k in ignore_raw):
        raise ConfigError("timetable.ignore_keywords must be a list of text values")

    timetable = TimetableConfig(
        path=timetable_path,
        lead_minutes=_int(tt, "lead_minutes", 45, 0, "timetable"),
        term_start=term_start,
        term_end=term_end,
        skip_dates=frozenset(_date(d, "timetable.skip_dates") for d in skip_raw),
        ignore_keywords=tuple(k.strip().lower() for k in ignore_raw if k.strip()),
    )

    al = raw.get("alarm", {})
    sound = _str(al, "sound_file", "", "alarm")
    sound_path = None
    if sound:
        sound_path = Path(sound).expanduser()
        if not sound_path.is_absolute():
            sound_path = base / sound_path
    alarm = AlarmConfig(
        snooze_minutes=_int(al, "snooze_minutes", 5, 1, "alarm"),
        max_snoozes=_int(al, "max_snoozes", 2, 0, "alarm"),
        ring_limit_minutes=_int(al, "ring_limit_minutes", 30, 1, "alarm"),
        dismiss_code=_bool(al, "dismiss_code", True, "alarm"),
        sound_file=sound_path,
        os_wake=_bool(al, "os_wake", False, "alarm"),
    )

    ph = raw.get("phone", {})
    nt = ph.get("ntfy", {})
    po = ph.get("pushover", {})
    tw = ph.get("twilio", {})

    ntfy = NtfyConfig(
        enabled=_bool(nt, "enabled", False, "phone.ntfy"),
        server=_str(nt, "server", "https://ntfy.sh", "phone.ntfy").rstrip("/"),
        topic=_str(nt, "topic", "", "phone.ntfy"),
        token=_str(nt, "token", "", "phone.ntfy"),
    )
    _require(ntfy.enabled, {"topic": ntfy.topic}, "phone.ntfy")

    pushover = PushoverConfig(
        enabled=_bool(po, "enabled", False, "phone.pushover"),
        app_token=_str(po, "app_token", "", "phone.pushover"),
        user_key=_str(po, "user_key", "", "phone.pushover"),
        sound=_str(po, "sound", "persistent", "phone.pushover"),
        retry_seconds=_int(po, "retry_seconds", 30, 30, "phone.pushover"),
    )
    _require(
        pushover.enabled,
        {"app_token": pushover.app_token, "user_key": pushover.user_key},
        "phone.pushover",
    )

    twilio = TwilioConfig(
        enabled=_bool(tw, "enabled", False, "phone.twilio"),
        account_sid=_str(tw, "account_sid", "", "phone.twilio"),
        auth_token=_str(tw, "auth_token", "", "phone.twilio"),
        from_number=_str(tw, "from_number", "", "phone.twilio"),
        to_number=_str(tw, "to_number", "", "phone.twilio"),
        call_every_minutes=_int(tw, "call_every_minutes", 3, 1, "phone.twilio"),
    )
    _require(
        twilio.enabled,
        {
            "account_sid": twilio.account_sid,
            "auth_token": twilio.auth_token,
            "from_number": twilio.from_number,
            "to_number": twilio.to_number,
        },
        "phone.twilio",
    )

    phone = PhoneConfig(
        repeat_seconds=_int(ph, "repeat_seconds", 60, 15, "phone"),
        ntfy=ntfy,
        pushover=pushover,
        twilio=twilio,
    )

    from class_alarm.timetable import TimetableError, parse_days

    sh = raw.get("shampoo", {})
    days_raw = sh.get("days", [])
    if not isinstance(days_raw, list) or not all(isinstance(d, str) for d in days_raw):
        raise ConfigError('shampoo.days must be a list like ["Mon", "Thu"]')
    try:
        shampoo_days = tuple(sorted({d for text in days_raw for d in parse_days(text)}))
    except TimetableError as exc:
        raise ConfigError(f"shampoo.days: {exc}") from None
    shampoo = ShampooConfig(days=shampoo_days, lead_minutes=_int(sh, "lead_minutes", 75, 0, "shampoo"))

    be = raw.get("behavior", {})
    behavior = BehaviorConfig(
        enabled=_bool(be, "enabled", False, "behavior"),
        server=_str(be, "server", "https://ntfy.sh", "behavior").rstrip("/"),
        events_topic=_str(be, "events_topic", "", "behavior"),
        token=_str(be, "token", "", "behavior"),
    )
    _require(behavior.enabled, {"events_topic": behavior.events_topic}, "behavior")
    if behavior.enabled and ntfy.enabled and behavior.events_topic == ntfy.topic:
        raise ConfigError("behavior.events_topic must be different from phone.ntfy.topic")

    sl = raw.get("sleep", {})
    sleep = SleepConfig(
        enabled=_bool(sl, "enabled", False, "sleep"),
        sleep_hours=_float(sl, "sleep_hours", 7.5, 3, 12, "sleep"),
        fall_asleep_minutes=_int(sl, "fall_asleep_minutes", 15, 0, "sleep"),
        wind_down_minutes=_int(sl, "wind_down_minutes", 30, 0, "sleep"),
        nag_minutes=_int(sl, "nag_minutes", 30, 10, "sleep"),
        max_nags=_int(sl, "max_nags", 4, 0, "sleep"),
    )

    aw = raw.get("awake_check", {})
    awake_check = AwakeCheckConfig(
        enabled=_bool(aw, "enabled", False, "awake_check"),
        check_after_minutes=_int(aw, "check_after_minutes", 5, 0, "awake_check"),
        confirm_window_minutes=_int(aw, "confirm_window_minutes", 5, 1, "awake_check"),
        max_rerings=_int(aw, "max_rerings", 2, 0, "awake_check"),
    )

    can_message = ntfy.enabled or pushover.enabled
    if sleep.enabled and not can_message:
        raise ConfigError("sleep reminders need phone.ntfy or phone.pushover enabled")
    if awake_check.enabled and not (pushover.enabled or (ntfy.enabled and behavior.enabled)):
        raise ConfigError(
            "awake_check needs a way for you to answer: enable phone.pushover, "
            "or enable both phone.ntfy and behavior"
        )

    st = raw.get("storage", {})
    data_dir = Path(_str(st, "dir", "data", "storage") or "data").expanduser()
    if not data_dir.is_absolute():
        data_dir = base / data_dir

    return Config(
        timetable=timetable,
        alarm=alarm,
        phone=phone,
        shampoo=shampoo,
        behavior=behavior,
        sleep=sleep,
        awake_check=awake_check,
        data_dir=data_dir,
    )

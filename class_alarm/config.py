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
class Config:
    timetable: TimetableConfig
    alarm: AlarmConfig
    phone: PhoneConfig


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

    return Config(timetable=timetable, alarm=alarm, phone=phone)

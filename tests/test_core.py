import json
import urllib.parse
from datetime import date, datetime, time
from pathlib import Path

import pytest

from class_alarm.config import AlarmConfig, ConfigError, TimetableConfig, load_config
from class_alarm.notifiers import (
    NtfyNotifier,
    PushoverNotifier,
    TwilioCallNotifier,
    build_notifiers,
)
from class_alarm.planner import plan_alarms, upcoming_alarms
from class_alarm.ringer import ring
from class_alarm.timetable import (
    TimetableError,
    parse_days,
    parse_time,
    sessions_between,
)
from class_alarm.wake import wake_command

ROOT = Path(__file__).resolve().parent.parent


def local(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm).astimezone()


def tt_cfg(path, **kw):
    return TimetableConfig(path=Path(path), **kw)


# --- parsing -----------------------------------------------------------------

def test_parse_days():
    assert parse_days("Mon/Wed/Fri") == (0, 2, 4)
    assert parse_days("tue thu") == (1, 3)
    assert parse_days("Thursday") == (3,)
    assert parse_days("weekdays") == (0, 1, 2, 3, 4)
    with pytest.raises(TimetableError):
        parse_days("Funday")


@pytest.mark.parametrize(
    "text,expected",
    [("07:30", time(7, 30)), ("7:30", time(7, 30)), ("7:30 AM", time(7, 30)),
     ("7:30pm", time(19, 30)), ("12:15 PM", time(12, 15)), ("19:05", time(19, 5)),
     ("9 AM", time(9, 0))],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


def test_parse_time_rejects_garbage():
    with pytest.raises(TimetableError):
        parse_time("soon")


# --- the core rule: 45 minutes before the first class of the day -----------------

def test_example_timetable_7_30_class_wakes_at_6_45():
    cfg = tt_cfg(ROOT / "timetable.example.csv")
    # 2026-09-21 is a Monday.
    sessions = sessions_between(cfg, date(2026, 9, 21), date(2026, 9, 27))
    alarms = plan_alarms(sessions, 45)
    by_day = {a.wake_at.date(): a for a in alarms}

    mon = by_day[date(2026, 9, 21)]
    assert mon.first_class.name == "MATH 101"
    assert (mon.wake_at.hour, mon.wake_at.minute) == (6, 45)

    tue = by_day[date(2026, 9, 22)]  # 09:00 ENGL is earlier than the 13:00 lab
    assert tue.first_class.name == "ENGL 102"
    assert (tue.wake_at.hour, tue.wake_at.minute) == (8, 15)

    assert date(2026, 9, 26) not in by_day  # Saturday, no class
    assert date(2026, 9, 27) not in by_day  # Sunday


def test_lead_crossing_midnight(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("day,start,course\nMon,00:30,Night lab\n")
    alarms = plan_alarms(sessions_between(tt_cfg(f), date(2026, 9, 21), date(2026, 9, 21)), 45)
    assert alarms[0].wake_at == local(2026, 9, 20, 23, 45)


def test_filters_term_skip_and_keywords():
    cfg = tt_cfg(
        ROOT / "timetable.example.csv",
        term_start=date(2026, 9, 22),
        skip_dates=frozenset({date(2026, 9, 23)}),
        ignore_keywords=("office hours", "lab"),
    )
    sessions = sessions_between(cfg, date(2026, 9, 21), date(2026, 9, 25))
    days = {s.start.date() for s in sessions}
    assert date(2026, 9, 21) not in days  # before term
    assert date(2026, 9, 23) not in days  # skipped
    assert not any("Office Hours" in s.name or "Lab" in s.name for s in sessions)


def test_office_hours_ignored_does_not_move_friday_alarm():
    cfg = tt_cfg(ROOT / "timetable.example.csv", ignore_keywords=("office hours",))
    alarms = plan_alarms(sessions_between(cfg, date(2026, 9, 25), date(2026, 9, 25)), 45)
    assert alarms[0].first_class.name == "MATH 101"


def test_upcoming_skips_classes_already_started():
    cfg = tt_cfg(ROOT / "timetable.example.csv")
    now = local(2026, 9, 21, 7, 0)  # Monday, after 6:45 wake, before 7:30 class
    alarms = upcoming_alarms(cfg, now)
    assert alarms[0].first_class.start == local(2026, 9, 21, 7, 30)  # still rings: you're late
    now = local(2026, 9, 21, 8, 0)  # class started: next alarm is tomorrow
    assert upcoming_alarms(cfg, now)[0].wake_at == local(2026, 9, 22, 8, 15)


def test_csv_errors_name_the_line(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("day,start\nMon,07:30\nBlursday,08:00\n")
    with pytest.raises(TimetableError, match="line 3"):
        sessions_between(tt_cfg(f), date(2026, 9, 21), date(2026, 9, 27))
    f.write_text("when,time\nMon,07:30\n")
    with pytest.raises(TimetableError, match="'day' and 'start'"):
        sessions_between(tt_cfg(f), date(2026, 9, 21), date(2026, 9, 27))


def test_ics_timetable(tmp_path):
    f = tmp_path / "t.ics"
    f.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:MATH 101\r\nLOCATION:Room 204\r\n"
        "DTSTART:20260921T073000\r\nDTEND:20260921T084500\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\r\nEXDATE:20260923T073000\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Holiday\r\n"
        "DTSTART;VALUE=DATE:20260922\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:3\r\nSUMMARY:Cancelled seminar\r\nSTATUS:CANCELLED\r\n"
        "DTSTART:20260922T060000\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    sessions = sessions_between(tt_cfg(f), date(2026, 9, 21), date(2026, 9, 27))
    assert [s.start for s in sessions] == [local(2026, 9, 21, 7, 30), local(2026, 9, 25, 7, 30)]
    assert sessions[0].location == "Room 204"
    assert plan_alarms(sessions, 45)[0].wake_at == local(2026, 9, 21, 6, 45)


# --- config ------------------------------------------------------------------

def test_example_config_loads(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text((ROOT / "config.example.toml").read_text())
    cfg = load_config(cfg_file)
    assert cfg.timetable.lead_minutes == 45
    assert cfg.timetable.path == tmp_path / "timetable.csv"
    assert cfg.phone.ntfy.enabled and not cfg.phone.pushover.enabled


def test_config_rejects_enabled_channel_without_keys(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[timetable]\npath = "t.csv"\n[phone.pushover]\nenabled = true\n')
    with pytest.raises(ConfigError, match="app_token"):
        load_config(f)


def test_config_accepts_toml_and_string_dates(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[timetable]\npath = "t.csv"\nterm_start = 2026-08-24\nterm_end = "2026-12-11"\n')
    cfg = load_config(f)
    assert cfg.timetable.term_start == date(2026, 8, 24)
    assert cfg.timetable.term_end == date(2026, 12, 11)


# --- phone channels ----------------------------------------------------------

class Recorder:
    def __init__(self, response=b"{}"):
        self.requests = []
        self.response = response

    def __call__(self, req):
        self.requests.append(req)
        return self.response


def test_ntfy_request_and_repeat():
    from class_alarm.config import NtfyConfig

    rec = Recorder()
    n = NtfyNotifier(NtfyConfig(enabled=True, topic="my topic"), interval=60, sender=rec)
    n.start("Wake up!", now=0)
    n.tick("Wake up!", now=30)
    n.tick("Wake up!", now=61)
    assert len(rec.requests) == 2
    req = rec.requests[0]
    assert req.url == "https://ntfy.sh/my%20topic"
    assert req.headers["Priority"] == "5"
    assert req.data == b"Wake up!"
    n.stop()
    n.tick("Wake up!", now=500)
    assert len(rec.requests) == 2


def test_pushover_emergency_and_cancel_on_dismiss():
    from class_alarm.config import PushoverConfig

    rec = Recorder(json.dumps({"status": 1, "receipt": "abc123"}).encode())
    n = PushoverNotifier(PushoverConfig(enabled=True, app_token="T", user_key="U"), sender=rec)
    n.start("Wake up!", now=0)
    n.tick("Wake up!", now=1000)  # Pushover repeats on its own; we must not resend
    assert len(rec.requests) == 1
    fields = urllib.parse.parse_qs(rec.requests[0].data.decode())
    assert fields["priority"] == ["2"] and fields["retry"] == ["30"]
    n.stop()
    assert rec.requests[1].url == "https://api.pushover.net/1/receipts/abc123/cancel.json"


def test_twilio_call_escapes_message():
    from class_alarm.config import TwilioConfig

    rec = Recorder()
    cfg = TwilioConfig(enabled=True, account_sid="AC1", auth_token="x", from_number="+1", to_number="+2")
    n = TwilioCallNotifier(cfg, sender=rec)
    n.start("R&D <lab>", now=0)
    fields = urllib.parse.parse_qs(rec.requests[0].data.decode())
    assert fields["Twiml"] == ['<Response><Say loop="3">R&amp;D &lt;lab&gt;</Say></Response>']
    assert rec.requests[0].url.endswith("/Accounts/AC1/Calls.json")
    assert n.interval == 180


def test_failing_channel_does_not_raise():
    from class_alarm.config import NtfyConfig

    def boom(req):
        raise OSError("network down")

    NtfyNotifier(NtfyConfig(enabled=True, topic="t"), interval=60, sender=boom).start("x", now=0)


def test_build_notifiers_only_enabled():
    from class_alarm.config import PhoneConfig

    assert build_notifiers(PhoneConfig()) == []


# --- ringing loop ------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeReader:
    """Feeds scripted input; each get() advances the fake clock by the poll interval."""

    def __init__(self, clock, lines):
        import threading

        self.clock = clock
        self.lines = list(lines)  # (at_time, text)
        self.closed = threading.Event()

    def drain(self):
        pass

    def get(self, timeout):
        self.clock.t += timeout
        if self.lines and self.clock.t >= self.lines[0][0]:
            return self.lines.pop(0)[1]
        return None


class FakeSound:
    def __init__(self):
        self.playing = False
        self.starts = 0

    def start(self):
        self.playing = True
        self.starts += 1

    def stop(self):
        self.playing = False


def run_ring(cfg, lines, monkeypatch):
    monkeypatch.setattr("class_alarm.ringer.random.randint", lambda a, b: 4821)
    clock, sound, out = FakeClock(), FakeSound(), []
    result = ring("Wake up!", cfg, [], sound, FakeReader(clock, lines), clock=clock, out=out.append)
    return result, sound, out, clock


def test_ring_needs_the_code(monkeypatch):
    result, sound, out, _ = run_ring(AlarmConfig(), [(5, "1234"), (10, "4821")], monkeypatch)
    assert result == "dismissed" and not sound.playing
    assert any("Wrong code" in line for line in out)


def test_ring_snooze_then_rings_again(monkeypatch):
    cfg = AlarmConfig(snooze_minutes=5, max_snoozes=1)
    result, sound, out, clock = run_ring(cfg, [(3, "s"), (400, "s"), (410, "4821")], monkeypatch)
    assert result == "dismissed"
    assert sound.starts == 2  # rang, snoozed 5 min, rang again
    assert any("No snoozes left" in line for line in out)


def test_ring_times_out(monkeypatch):
    cfg = AlarmConfig(ring_limit_minutes=1)
    result, sound, _, clock = run_ring(cfg, [], monkeypatch)
    assert result == "timed_out" and not sound.playing
    assert clock.t == pytest.approx(60, abs=1)


def test_ring_enter_dismisses_without_code(monkeypatch):
    result, _, _, _ = run_ring(AlarmConfig(dismiss_code=False), [(2, "")], monkeypatch)
    assert result == "dismissed"


# --- OS wake commands --------------------------------------------------------

def test_wake_commands():
    when = local(2026, 9, 22, 6, 43)
    assert wake_command(when, "darwin") == ["pmset", "schedule", "wake", "09/22/26 06:43:00"]
    assert wake_command(when, "linux") == ["rtcwake", "-m", "no", "-t", str(int(when.timestamp()))]
    win = wake_command(when, "win32")
    assert win[0] == "powershell" and "-WakeToRun" in win[-1] and "2026-09-22T06:43:00" in win[-1]

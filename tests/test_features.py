import json
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from class_alarm import cli
from class_alarm.awake import confirm_awake
from class_alarm.behavior import EventFeed, SleepCoach, learn_pattern, projected_quiet, sleep_period
from class_alarm.config import (
    AwakeCheckConfig,
    BehaviorConfig,
    ConfigError,
    NtfyConfig,
    PhoneConfig,
    PushoverConfig,
    ShampooConfig,
    SleepConfig,
    TimetableConfig,
    load_config,
)
from class_alarm.notifiers import Messenger
from class_alarm.planner import lead_function, plan_alarms, upcoming_alarms
from class_alarm.state import Event, EventLog, State, StateStore
from class_alarm.timetable import sessions_between

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = TimetableConfig(path=ROOT / "timetable.example.csv")


def local(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm).astimezone()


def ts(y, m, d, hh, mm):
    return local(y, m, d, hh, mm).timestamp()


# --- shampoo -----------------------------------------------------------------

def week_alarms(lead):
    sessions = sessions_between(EXAMPLE, date(2026, 9, 21), date(2026, 9, 25))
    return {a.first_class.start.date(): a for a in plan_alarms(sessions, lead)}


def test_weekly_shampoo_day_wakes_75_minutes_early():
    alarms = week_alarms(lead_function(45, ShampooConfig(days=(0,), lead_minutes=75)))
    mon, wed = alarms[date(2026, 9, 21)], alarms[date(2026, 9, 23)]
    assert mon.shampoo and mon.wake_at == local(2026, 9, 21, 6, 15)  # 7:30 class
    assert not wed.shampoo and wed.wake_at == local(2026, 9, 23, 6, 45)
    assert "Shampoo day" in mon.message()


def test_one_off_shampoo_overrides():
    state = State()
    state.set_shampoo(date(2026, 9, 22), True)  # Tue one-off
    state.set_shampoo(date(2026, 9, 21), False)  # skip this Monday's weekly shampoo
    alarms = week_alarms(lead_function(45, ShampooConfig(days=(0,), lead_minutes=75), state))
    assert alarms[date(2026, 9, 22)].wake_at == local(2026, 9, 22, 7, 45)  # 9:00 class
    assert alarms[date(2026, 9, 21)].wake_at == local(2026, 9, 21, 6, 45)
    state.set_shampoo(date(2026, 9, 21), True)  # turning it back on replaces the off
    assert state.is_shampoo_override(date(2026, 9, 21)) is True


def test_alarm_key_does_not_change_with_wake_time():
    normal = week_alarms(45)[date(2026, 9, 21)]
    shampoo = week_alarms(75)[date(2026, 9, 21)]
    assert normal.wake_at != shampoo.wake_at and normal.key == shampoo.key


def test_shampoo_config(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[timetable]\npath = "t.csv"\n[shampoo]\ndays = ["Mon/Thu"]\nlead_minutes = 80\n')
    cfg = load_config(f)
    assert cfg.shampoo.days == (0, 3) and cfg.shampoo.lead_minutes == 80
    f.write_text('[timetable]\npath = "t.csv"\n[shampoo]\ndays = ["Someday"]\n')
    with pytest.raises(ConfigError, match="shampoo.days"):
        load_config(f)


def test_state_roundtrip(tmp_path):
    store = StateStore(tmp_path)
    today = date.today()
    store.update(lambda s: s.set_shampoo(today, True))
    store.update(lambda s: s.seen_ids.append("abc"))
    loaded = store.load()
    assert loaded.shampoo_on == {today} and loaded.seen_ids == ["abc"]


# --- config validation -------------------------------------------------------

def test_awake_check_needs_a_way_to_answer(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        '[timetable]\npath = "t.csv"\n[phone.ntfy]\nenabled = true\ntopic = "a"\n[awake_check]\nenabled = true\n'
    )
    with pytest.raises(ConfigError, match="awake_check"):
        load_config(f)
    f.write_text(
        '[timetable]\npath = "t.csv"\n[phone.ntfy]\nenabled = true\ntopic = "a"\n'
        '[behavior]\nenabled = true\nevents_topic = "b"\n[awake_check]\nenabled = true\n'
    )
    assert load_config(f).awake_check.enabled


def test_events_topic_must_differ(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        '[timetable]\npath = "t.csv"\n[phone.ntfy]\nenabled = true\ntopic = "same"\n'
        '[behavior]\nenabled = true\nevents_topic = "same"\n'
    )
    with pytest.raises(ConfigError, match="different"):
        load_config(f)


def test_full_example_config_loads(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text((ROOT / "config.example.toml").read_text())
    cfg = load_config(f)
    assert cfg.shampoo.lead_minutes == 75
    assert cfg.data_dir == tmp_path / "data"


# --- phone event feed --------------------------------------------------------

def ntfy_lines(*msgs):
    return "\n".join(json.dumps(m) for m in msgs).encode()


def test_event_feed_logs_new_events_once(tmp_path):
    body = ntfy_lines(
        {"id": "o1", "event": "open", "time": 1},
        {"id": "m1", "event": "message", "time": 1000, "message": "app_open"},
        {"id": "m2", "event": "message", "time": 1060, "message": "Charger_On\n"},
        {"id": "m3", "event": "message", "time": 1070, "message": "dance"},
    )
    urls = []

    def getter(url, headers):
        urls.append(url)
        return body

    store, log = StateStore(tmp_path), EventLog(tmp_path)
    feed = EventFeed(BehaviorConfig(enabled=True, events_topic="ev"), store, log, getter)
    new = feed.poll()
    assert [(e.kind, e.ts) for e in new] == [("app_open", 1000.0), ("charger_on", 1060.0)]
    assert feed.poll() == []  # same messages again: nothing new
    assert len(log.read()) == 2
    assert "m3" in store.load().seen_ids  # unknown word isn't re-read every poll
    assert urls[0].startswith("https://ntfy.sh/ev/json?poll=1&since=")


def test_event_feed_survives_network_errors(tmp_path):
    def getter(url, headers):
        raise OSError("offline")

    feed = EventFeed(BehaviorConfig(enabled=True, events_topic="ev"), StateStore(tmp_path), EventLog(tmp_path), getter)
    assert feed.poll() == []


# --- sleep pattern -----------------------------------------------------------

def night_events(night: date, quiet_hm, wake_hm):
    """Evening activity up to quiet time, then first use the next morning."""
    d = night
    q = datetime.combine(d, datetime.min.time()).replace(hour=quiet_hm[0], minute=quiet_hm[1]).astimezone()
    if quiet_hm[0] < 18:
        q += timedelta(days=1)
    w = datetime.combine(d + timedelta(days=1), datetime.min.time()).replace(hour=wake_hm[0], minute=wake_hm[1]).astimezone()
    return [
        Event(ts=(q - timedelta(minutes=40)).timestamp(), kind="app_open"),
        Event(ts=(q - timedelta(minutes=5)).timestamp(), kind="charger_on"),
        Event(ts=q.timestamp(), kind="app_close"),
        Event(ts=w.timestamp(), kind="charger_off"),
        Event(ts=(w + timedelta(minutes=3)).timestamp(), kind="app_open"),
    ]


def test_sleep_period_crosses_midnight():
    events = night_events(date(2026, 9, 20), (1, 30), (7, 10))
    quiet, first = sleep_period(events, date(2026, 9, 20))
    assert quiet == local(2026, 9, 21, 1, 30) and first == local(2026, 9, 21, 7, 10)


def test_sleep_period_needs_a_morning_event():
    events = night_events(date(2026, 9, 20), (23, 0), (7, 0))[:3]  # nothing the next morning
    assert sleep_period(events, date(2026, 9, 20)) is None


def test_learn_pattern_median_across_midnight():
    events = []
    for i, quiet in enumerate([(23, 30), (0, 30), (1, 30), (0, 45)]):
        events += night_events(date(2026, 9, 17) + timedelta(days=i), quiet, (7, 30))
    pattern = learn_pattern(events, date(2026, 9, 21))
    assert pattern.nights == 4
    assert (pattern.usual_quiet.hour, pattern.usual_quiet.minute) == (0, 37)  # median of 0:30 and 0:45
    assert projected_quiet(pattern, local(2026, 9, 22, 6, 45)) == local(2026, 9, 22, 0, 37)


def test_learn_pattern_needs_three_nights():
    events = night_events(date(2026, 9, 19), (23, 0), (7, 0)) + night_events(date(2026, 9, 20), (23, 0), (7, 0))
    assert learn_pattern(events, date(2026, 9, 21)) is None


# --- bedtime reminders -------------------------------------------------------

def test_sleep_coach_reminders():
    coach = SleepCoach(SleepConfig(enabled=True, sleep_hours=7.5, fall_asleep_minutes=15, wind_down_minutes=30, nag_minutes=30, max_nags=2))
    wake = local(2026, 9, 22, 6, 45)
    key = "k"
    assert coach.bedtime(wake) == local(2026, 9, 21, 23, 0)

    events = []
    for i, quiet in enumerate([(1, 0), (1, 30), (0, 45)]):
        events += night_events(date(2026, 9, 18) + timedelta(days=i), quiet, (8, 0))
    pattern = learn_pattern(events, date(2026, 9, 21))

    assert coach.due(local(2026, 9, 21, 22, 0), wake, key, [], pattern) == []
    wind = coach.due(local(2026, 9, 21, 22, 30), wake, key, [], pattern)
    assert len(wind) == 1 and "11:00 PM" in wind[0] and "1:00 AM" in wind[0] and "5h 45m" in wind[0]
    assert coach.due(local(2026, 9, 21, 22, 31), wake, key, [], pattern) == []  # once only

    bed = coach.due(local(2026, 9, 21, 23, 0), wake, key, [], pattern)
    assert bed == ["Time to sleep. Sleeping now gives you 7h 30m before your 6:45 AM alarm."]

    # No phone use after bedtime: no nag.
    assert coach.due(local(2026, 9, 21, 23, 45), wake, key, [], pattern) == []
    # Phone used at 23:40: nag, then at most max_nags.
    used = [local(2026, 9, 21, 23, 40)]
    nag = coach.due(local(2026, 9, 21, 23, 45), wake, key, used, pattern)
    assert len(nag) == 1 and "Still on your phone" in nag[0] and "6h 45m" in nag[0]
    assert coach.due(local(2026, 9, 21, 23, 50), wake, key, used + [local(2026, 9, 21, 23, 49)], pattern) == []
    assert len(coach.due(local(2026, 9, 22, 0, 20), wake, key, [local(2026, 9, 22, 0, 10)], pattern)) == 1
    assert coach.due(local(2026, 9, 22, 1, 0), wake, key, [local(2026, 9, 22, 0, 55)], pattern) == []


def test_sleep_coach_ignores_far_alarms():
    coach = SleepCoach(SleepConfig(enabled=True))
    assert coach.due(local(2026, 9, 21, 8, 0), local(2026, 9, 23, 6, 45), "k", [], None) == []


# --- messenger ---------------------------------------------------------------

class Recorder:
    def __init__(self, response=b"{}"):
        self.requests = []
        self.response = response

    def __call__(self, req):
        self.requests.append(req)
        return self.response


def test_ntfy_awake_check_has_im_awake_button():
    rec = Recorder()
    phone = PhoneConfig(ntfy=NtfyConfig(enabled=True, topic="alerts"))
    m = Messenger(phone, BehaviorConfig(enabled=True, events_topic="events"), sender=rec)
    m.start_awake_check("Are you up?", 300)
    actions = json.loads(rec.requests[0].headers["Actions"])
    assert actions[0]["url"] == "https://ntfy.sh/events" and actions[0]["body"] == "awake"
    assert rec.requests[0].url == "https://ntfy.sh/alerts"


def test_pushover_awake_check_ack_and_cancel():
    rec = Recorder(json.dumps({"receipt": "r1"}).encode())
    acks = []

    def getter(url, headers):
        acks.append(url)
        return json.dumps({"acknowledged": 1}).encode()

    phone = PhoneConfig(pushover=PushoverConfig(enabled=True, app_token="T", user_key="U"))
    m = Messenger(phone, BehaviorConfig(), sender=rec, getter=getter)
    m.start_awake_check("Are you up?", 300)
    fields = urllib.parse.parse_qs(rec.requests[0].data.decode())
    assert fields["priority"] == ["2"] and fields["expire"] == ["420"]
    assert m.awake_acknowledged()
    assert acks[0] == "https://api.pushover.net/1/receipts/r1.json?token=T"
    m.stop_awake_check()
    assert rec.requests[-1].url.endswith("/receipts/r1/cancel.json")


# --- awake check after dismissing -------------------------------------------

class FakeMessenger:
    def __init__(self, ack_after=None):
        self.started = None
        self.stopped = False
        self.ack_after = ack_after
        self.checks = 0

    def start_awake_check(self, message, window):
        self.started = message

    def awake_acknowledged(self):
        self.checks += 1
        return self.ack_after is not None and self.checks >= self.ack_after

    def stop_awake_check(self):
        self.stopped = True


def run_check(messenger, events_at=()):
    """events_at: list of (fake time, kind) the phone reports."""
    clock = [1000.0]
    pending = sorted(events_at)

    def poll():
        out = []
        while pending and pending[0][0] <= clock[0]:
            t, kind = pending.pop(0)
            out.append(Event(ts=t, kind=kind))
        return out

    def sleep(s):
        clock[0] += s

    cfg = AwakeCheckConfig(enabled=True, check_after_minutes=5, confirm_window_minutes=5)
    result = confirm_awake(cfg, messenger, poll, wall=lambda: clock[0], sleep=sleep, out=lambda s: None)
    return result, clock[0]


def test_awake_check_times_out_and_cleans_up():
    m = FakeMessenger()
    result, end = run_check(m)
    assert result is False and m.stopped and m.started
    assert end == pytest.approx(1000 + 600)


def test_awake_check_passes_on_acknowledge():
    m = FakeMessenger(ack_after=3)
    assert run_check(m)[0] is True and m.stopped


def test_awake_check_passes_on_phone_activity_after_the_check():
    assert run_check(FakeMessenger(), [(1400, "app_open")])[0] is True


def test_activity_before_the_check_does_not_count():
    assert run_check(FakeMessenger(), [(1100, "app_open")])[0] is False


def test_going_to_bed_signals_do_not_count_as_awake():
    assert run_check(FakeMessenger(), [(1400, "charger_on"), (1410, "sleep_focus_on")])[0] is False


# --- shampoo from the phone --------------------------------------------------

def test_shampoo_shortcut_from_phone_moves_next_alarm(tmp_path, monkeypatch):
    (tmp_path / "t.csv").write_text((ROOT / "timetable.example.csv").read_text())
    (tmp_path / "config.toml").write_text(
        '[timetable]\npath = "t.csv"\n[phone.ntfy]\nenabled = true\ntopic = "alerts"\n'
        '[behavior]\nenabled = true\nevents_topic = "events"\n'
    )
    monkeypatch.setattr(State, "prune", lambda self, today: None)  # fixed 2026 dates in this test
    sent = Recorder()
    # Sunday evening: next class is Monday's 7:30 MATH 101.
    event_time = ts(2026, 9, 20, 21, 0)
    body = ntfy_lines({"id": "s1", "event": "message", "time": event_time, "message": "shampoo"})
    monkeypatch.setattr(cli, "EventFeed", lambda cfg, store, log: EventFeed(cfg, store, log, lambda u, h: body))
    monkeypatch.setattr(cli, "Messenger", lambda phone, beh: Messenger(phone, beh, sender=sent))

    app = cli.App(tmp_path / "config.toml")
    app.poll_events()
    assert app.store.load().shampoo_on == {date(2026, 9, 21)}
    monday = upcoming_alarms(
        app.cfg.timetable, local(2026, 9, 20, 21, 0),
        lead=lead_function(45, app.cfg.shampoo, app.store.load()),
    )[0]
    assert monday.wake_at == local(2026, 9, 21, 6, 15)
    assert b"6:15 AM" in sent.requests[0].data

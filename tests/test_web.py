import json
import struct
import urllib.error
import urllib.request
import zlib
from datetime import date
from pathlib import Path

import pytest

from class_alarm import cli
from class_alarm.ringer import LineReader
from class_alarm.web import WebServer, _Handler, load_or_create_key, make_icon, validate_rows

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def as_phone(monkeypatch):
    """Requests in tests come from 127.0.0.1; pretend they come from a phone on the Wi-Fi."""
    monkeypatch.setattr(_Handler, "_from_laptop", lambda self: False)


@pytest.fixture
def server(tmp_path):
    (tmp_path / "timetable.csv").write_text((ROOT / "timetable.example.csv").read_text())
    (tmp_path / "config.toml").write_text(
        '[timetable]\npath = "timetable.csv"\n[web]\nenabled = true\nhost = "127.0.0.1"\nport = 1\n'
    )
    app = cli.App(tmp_path / "config.toml")
    app.cfg.web.port = 0  # any free port
    srv = WebServer(app)
    srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    yield app, srv, base
    srv.stop()


def call(base, path, key=None, body=None, content_type="application/json"):
    headers = {"X-Key": key} if key else {}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_key_is_created_once(tmp_path):
    k1 = load_or_create_key(tmp_path)
    assert len(k1) >= 20 and load_or_create_key(tmp_path) == k1


def test_page_and_assets_are_public_but_api_needs_key(server):
    app, srv, base = server
    status, headers, body = call(base, "/")
    assert status == 200 and b"<title>Class Alarm</title>" in body
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    for path in ("/app.js", "/app.css", "/manifest.webmanifest"):
        assert call(base, path)[0] == 200
    assert call(base, "/api/state")[0] == 401
    assert call(base, "/api/state", key="wrong")[0] == 401
    assert call(base, "/api/awake", key="wrong", body={})[0] == 401
    assert call(base, "/nope")[0] == 404


def test_state(server, as_phone):
    app, srv, base = server
    status, _, body = call(base, "/api/state", key=srv.key)
    state = json.loads(body)
    assert status == 200 and state["error"] is None
    assert state["lead"] == 45 and state["status"] == {"ringing": False, "message": None, "awake_check": False}
    assert state["timetable"]["editable"] and state["timetable"]["rows"][0]["course"] == "MATH 101"
    assert len(state["alarms"]) >= 4
    assert state["next"]["wake"] and state["sleep"]["bedtime"]


def test_live_status_shows_ringing_and_awake_check(server, as_phone):
    app, srv, base = server
    app.status.set(ringing="Wake up! MATH 101 starts at 07:30.")
    s = json.loads(call(base, "/api/state", key=srv.key)[2])["status"]
    assert s["ringing"] and s["message"].startswith("Wake up")
    app.status.set(awake_check=True)
    s = json.loads(call(base, "/api/state", key=srv.key)[2])["status"]
    assert s == {"ringing": False, "message": None, "awake_check": True}


def test_shampoo_toggle(server):
    app, srv, base = server
    day = date.today().isoformat()
    assert call(base, "/api/shampoo", key=srv.key, body={"date": day, "on": True})[0] == 200
    assert app.store.load().shampoo_on == {date.today()}
    assert call(base, "/api/shampoo", key=srv.key, body={"date": day, "on": False})[0] == 200
    assert app.store.load().shampoo_off == {date.today()}
    assert call(base, "/api/shampoo", key=srv.key, body={"date": "soon", "on": True})[0] == 400
    assert call(base, "/api/shampoo", key=srv.key, body={"date": day, "on": "yes"})[0] == 400


def test_awake_reaches_the_alarm_loop(server):
    app, srv, base = server
    assert call(base, "/api/awake", key=srv.key, body={})[0] == 200
    events = app.poll_events()
    assert [e.kind for e in events] == ["awake"]
    assert app.poll_events() == []  # delivered once
    assert app.events.read()[-1].kind == "awake"  # and logged for the sleep pattern


def test_post_requires_json(server):
    app, srv, base = server
    assert call(base, "/api/awake", key=srv.key, body={}, content_type="text/plain")[0] == 415


def test_timetable_save_validates_and_backs_up(server):
    app, srv, base = server
    path = app.cfg.timetable.path
    original = path.read_text()

    bad = {"rows": [{"day": "Blursday", "start": "07:30", "course": "X"}]}
    status, _, body = call(base, "/api/timetable", key=srv.key, body=bad)
    assert status == 400 and b"blursday" in body
    assert path.read_text() == original  # untouched

    rows = [
        {"day": "Mon/Wed", "start": "8:00 AM", "end": "", "course": "BIO, Section 2", "location": "Lab 1"},
        {"day": "", "start": "", "course": ""},  # blank row the user left: dropped
    ]
    assert call(base, "/api/timetable", key=srv.key, body={"rows": rows})[0] == 200
    assert path.with_name("timetable.csv.bak").read_text() == original
    state = json.loads(call(base, "/api/state", key=srv.key)[2])
    assert state["timetable"]["rows"] == [
        {"day": "Mon/Wed", "start": "8:00 AM", "end": "", "course": "BIO, Section 2", "location": "Lab 1"}
    ]
    assert all(a["class"] == "BIO, Section 2" for a in state["alarms"])


def test_validate_rows_limits():
    with pytest.raises(ValueError):
        validate_rows("nope")
    with pytest.raises(ValueError, match="at most"):
        validate_rows([{"day": "Mon", "start": "07:00"}] * 101)
    with pytest.raises(ValueError, match="End"):
        validate_rows([{"day": "Mon", "start": "07:00", "end": "late", "course": "End"}])


def test_icons_are_valid_png(server):
    app, srv, base = server
    for path, size in (("/icon-180.png", 180), ("/icon-512.png", 512), ("/favicon.png", 32)):
        status, headers, body = call(base, path)
        assert status == 200 and headers["Content-Type"] == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", body[16:24]) == (size, size)


def test_icon_pixels_decode():
    png = make_icon(32)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    raw = zlib.decompress(idat)
    assert len(raw) == 32 * (1 + 32 * 4)


# --- laptop-only screen --------------------------------------------------------

class FakeReader:
    def __init__(self):
        self.lines = []

    def push(self, line):
        self.lines.append(line)


def ringing(app, code="4821", snoozes_left=1, phase="ringing"):
    app.reader = FakeReader()
    app.status.set(ringing="Wake up!")
    app.status.ringer_state({"phase": phase, "code": code, "snoozes_left": snoozes_left, "snooze_seconds": 300 if phase == "snoozed" else 0})


def test_only_the_laptop_sees_the_code(server, monkeypatch):
    app, srv, base = server
    ringing(app)
    laptop = json.loads(call(base, "/api/state", key=srv.key)[2])
    assert laptop["laptop"] and laptop["status"]["code"] == "4821" and laptop["status"]["phase"] == "ringing"
    monkeypatch.setattr(_Handler, "_from_laptop", lambda self: False)
    phone = json.loads(call(base, "/api/state", key=srv.key)[2])
    assert not phone["laptop"] and "code" not in phone["status"] and phone["phone_links"] == []


def test_phone_cannot_stop_snooze_or_test(server, as_phone):
    app, srv, base = server
    ringing(app)
    for path in ("/api/stop", "/api/snooze", "/api/test"):
        assert call(base, path, key=srv.key, body={"code": "4821"})[0] == 403
    assert app.reader.lines == [] and not app.test_requested


def test_laptop_stop_needs_the_right_code(server):
    app, srv, base = server
    ringing(app)
    status, _, body = call(base, "/api/stop", key=srv.key, body={"code": "1111"})
    assert status == 400 and b"Wrong code" in body and app.reader.lines == []
    assert call(base, "/api/stop", key=srv.key, body={"code": "4821"})[0] == 200
    assert app.reader.lines == ["4821"]


def test_laptop_stop_without_code_setting(server):
    app, srv, base = server
    ringing(app, code=None)
    assert call(base, "/api/stop", key=srv.key, body={})[0] == 200
    assert app.reader.lines == [""]


def test_laptop_snooze(server):
    app, srv, base = server
    ringing(app, snoozes_left=1)
    assert call(base, "/api/snooze", key=srv.key, body={})[0] == 200
    assert app.reader.lines == ["s"]
    ringing(app, snoozes_left=0)
    assert call(base, "/api/snooze", key=srv.key, body={})[0] == 400
    ringing(app, phase="snoozed")
    assert call(base, "/api/snooze", key=srv.key, body={})[0] == 409
    state = json.loads(call(base, "/api/state", key=srv.key)[2])
    assert state["status"]["phase"] == "snoozed" and state["status"]["snooze_until"]


def test_nothing_to_stop(server):
    app, srv, base = server
    app.reader = FakeReader()
    assert call(base, "/api/stop", key=srv.key, body={"code": "1"})[0] == 409


def test_laptop_test_alarm(server):
    app, srv, base = server
    assert call(base, "/api/test", key=srv.key, body={})[0] == 200
    assert app.test_requested and app.wakeup.is_set()
    ringing(app)
    assert call(base, "/api/test", key=srv.key, body={})[0] == 409


def test_laptop_view_is_remembered(server):
    app, srv, base = server
    assert app.last_laptop_view == 0
    call(base, "/api/state", key=srv.key)
    assert app.last_laptop_view > 0
    assert srv.local_link().startswith(f"http://localhost:{srv.port}/?key=")


def test_web_stop_really_stops_the_ringer():
    """The real ring loop, with the web app's push() instead of the keyboard."""
    import threading
    from class_alarm.config import AlarmConfig
    from class_alarm.ringer import ring

    class Sound:
        playing = False

        def start(self):
            self.playing = True

        def stop(self):
            self.playing = False

    reader = LineReader(stream=__import__("io").StringIO(""))  # no keyboard
    states = []
    sound = Sound()
    result = {}
    t = threading.Thread(target=lambda: result.update(r=ring(
        "Wake up!", AlarmConfig(ring_limit_minutes=1), [], sound, reader,
        out=lambda s: None, poll_seconds=0.05, on_state=states.append)))
    t.start()
    for _ in range(100):
        if states:
            break
        __import__("time").sleep(0.02)
    assert states[0]["phase"] == "ringing" and sound.playing
    reader.push(states[0]["code"])
    t.join(5)
    assert result["r"] == "dismissed" and not sound.playing

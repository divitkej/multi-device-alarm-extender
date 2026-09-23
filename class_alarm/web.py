"""The phone web app, served by the laptop over your Wi-Fi while `class-alarm run` is running.

Access needs a secret key that is part of the link printed on the laptop. The page itself holds
no data; every /api/ call must send the key.
"""

from __future__ import annotations

import csv
import functools
import hmac
import io
import json
import logging
import math
import secrets
import socket
import struct
import threading
import time
import zlib
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

from class_alarm.behavior import SleepCoach, fmt_clock, fmt_duration, learn_pattern, sleep_period
from class_alarm.state import _atomic_write
from class_alarm.timetable import TimetableError, parse_days, parse_time

if TYPE_CHECKING:
    from class_alarm.cli import App

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web"
MAX_BODY = 64 * 1024
MAX_ROWS = 100
CSV_COLUMNS = ("day", "start", "end", "course", "location")


# --- access key ------------------------------------------------------------------

def load_or_create_key(data_dir: Path) -> str:
    path = data_dir / "web_key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 20:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(24)
    _atomic_write(path, key)
    return key


def lan_addresses() -> list[str]:
    """Best guess at the laptop's address on the Wi-Fi, plus its .local name."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent; this just picks the Wi-Fi interface
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                found.append(ip)
    except OSError:
        pass
    host = socket.gethostname().split(".")[0]
    if host:
        found.append(f"{host}.local")
    return found or ["127.0.0.1"]


def links(key: str, port: int) -> list[str]:
    return [f"http://{addr}:{port}/?key={quote(key)}" for addr in lan_addresses()]


# --- timetable CSV editing ---------------------------------------------------------

def read_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    fields = {f.strip().lower(): f for f in (reader.fieldnames or []) if f}
    rows = []
    for row in reader:
        item = {c: (row.get(fields[c]) or "").strip() if c in fields else "" for c in CSV_COLUMNS}
        if not item["day"] and not item["start"]:
            continue
        if item["day"].startswith("#"):
            continue
        rows.append(item)
    return rows


def validate_rows(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("rows must be a list")
    if len(raw) > MAX_ROWS:
        raise ValueError(f"at most {MAX_ROWS} classes")
    rows = []
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"row {n}: invalid")
        row = {c: str(item.get(c, "") or "").strip()[:100] for c in CSV_COLUMNS}
        if not row["day"] and not row["start"] and not row["course"]:
            continue  # an empty row the user added and left blank
        try:
            parse_days(row["day"])
            parse_time(row["start"])
            if row["end"]:
                parse_time(row["end"])
        except TimetableError as exc:
            label = row["course"] or f"row {n}"
            raise ValueError(f"{label}: {exc}") from None
        if any(ch in row[c] for c in CSV_COLUMNS for ch in "\r\n"):
            raise ValueError(f"row {n}: line breaks are not allowed")
        rows.append(row)
    return rows


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if path.exists():
        _atomic_write(path.with_name(path.name + ".bak"), path.read_text(encoding="utf-8-sig"))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([row[c] for c in CSV_COLUMNS])
    _atomic_write(path, buf.getvalue())


# --- state for the page ---------------------------------------------------------------

def _alarm_json(a, now: datetime) -> dict:
    c = a.first_class
    return {
        "date": c.start.date().isoformat(),
        "day": f"{c.start:%a %d %b}",
        "wake": fmt_clock(a.wake_at),
        "wake_ts": a.wake_at.timestamp(),
        "wake_passed": a.wake_at <= now,
        "class": c.name,
        "class_time": fmt_clock(c.start),
        "location": c.location,
        "shampoo": a.shampoo,
    }


def build_state(app: App) -> dict:
    now = datetime.now().astimezone()
    cfg = app.cfg
    state: dict = {
        "now": now.isoformat(),
        "status": app.status.snapshot(),
        "lead": cfg.timetable.lead_minutes,
        "shampoo_lead": cfg.shampoo.lead_minutes,
        "error": None,
        "alarms": [],
        "next": None,
    }
    try:
        alarms = app.alarms(now, 7)
    except TimetableError as exc:
        state["error"] = str(exc)
        alarms = []
    state["alarms"] = [_alarm_json(a, now) for a in alarms]
    future = [a for a in alarms if a.wake_at > now]
    if future:
        nxt = future[0]
        state["next"] = {**_alarm_json(nxt, now), "in": fmt_duration(nxt.wake_at - now)}

    events = app.events.read(time.time() - 16 * 86400)
    today = now.date()
    pattern = learn_pattern(events, today)
    nights = []
    for i in range(7, 0, -1):
        period = sleep_period(events, today - timedelta(days=i))
        if period:
            quiet, first = period
            nights.append({
                "night": f"{today - timedelta(days=i):%a %d %b}",
                "quiet": fmt_clock(quiet),
                "first": fmt_clock(first),
                "duration": fmt_duration(first - quiet),
            })
    sleep: dict = {
        "enabled": cfg.sleep.enabled,
        "hours": cfg.sleep.sleep_hours,
        "bedtime": None,
        "for_alarm": None,
        "pattern": None,
        "nights": nights,
        "tracking": cfg.behavior.enabled,
    }
    if future:
        sleep["bedtime"] = fmt_clock(SleepCoach(cfg.sleep).bedtime(future[0].wake_at))
        sleep["for_alarm"] = f"{fmt_clock(future[0].wake_at)} {future[0].wake_at:%a}"
    if pattern:
        sleep["pattern"] = {"usual": fmt_clock(pattern.usual_quiet), "hours": round(pattern.usual_hours, 1), "nights": pattern.nights}
    state["sleep"] = sleep

    path = cfg.timetable.path
    editable = path.suffix.lower() == ".csv"
    timetable = {"editable": editable, "file": path.name, "rows": []}
    if editable and path.exists():
        try:
            timetable["rows"] = read_rows(path)
        except (OSError, csv.Error) as exc:
            state["error"] = state["error"] or f"can't read {path.name}: {exc}"
    state["timetable"] = timetable
    return state


# --- icons (drawn at runtime so no binary files are needed) -------------------------------

def _png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    stride = width * 4
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


@functools.lru_cache(maxsize=None)
def make_icon(size: int) -> bytes:
    """A plain clock face: dark square, light ring, hands at 6:45."""
    bg, fg, accent = (23, 28, 33), (240, 242, 244), (242, 160, 61)
    c = (size - 1) / 2
    ring_r, ring_w = size * 0.33, size * 0.055
    # hands: (angle from 12 o'clock in degrees, length, width, color)
    hands = [(202.5, size * 0.17, size * 0.05, fg), (270.0, size * 0.25, size * 0.035, accent)]

    def seg_dist(px, py, ang, length):
        dx, dy = math.sin(math.radians(ang)), -math.cos(math.radians(ang))
        t = max(0.0, min(length, (px - c) * dx + (py - c) * dy))
        return math.hypot(px - (c + dx * t), py - (c + dy * t))

    out = bytearray()
    for y in range(size):
        for x in range(size):
            color = bg
            d = math.hypot(x - c, y - c)
            cover = max(0.0, min(1.0, ring_w / 2 - abs(d - ring_r) + 0.5))
            if cover:
                color = tuple(round(b + (f - b) * cover) for b, f in zip(color, fg))
            for ang, length, width, col in hands:
                cover = max(0.0, min(1.0, width / 2 - seg_dist(x, y, ang, length) + 0.5))
                if cover:
                    color = tuple(round(b + (f - b) * cover) for b, f in zip(color, col))
            out += bytes((*color, 255))
    return _png(size, size, bytes(out))


# --- HTTP ------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "ClassAlarm"
    app: App
    key: str
    icons: dict[str, bytes]

    def log_message(self, format, *args):  # noqa: A002 - don't log URLs, they contain the key
        pass

    def _send(self, status: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _authorized(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Key", ""), self.key)

    STATIC = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/app.css": ("app.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    }

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in self.STATIC:
            name, ctype = self.STATIC[path]
            self._send(200, (STATIC_DIR / name).read_bytes(), ctype, cache=path != "/")
        elif path in self.icons:
            self._send(200, self.icons[path], "image/png", cache=True)
        elif path == "/api/state":
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            try:
                self._json(200, build_state(self.app))
            except Exception:
                log.exception("web: building state failed")
                self._json(500, {"error": "Something went wrong on the laptop. Check its terminal."})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self._json(415, {"error": "expected JSON"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "invalid JSON"})

        try:
            if path == "/api/shampoo":
                return self._shampoo(body)
            if path == "/api/awake":
                self.app.add_local_event("awake")
                log.info("web: 'I'm awake' tapped")
                return self._json(200, {"ok": True})
            if path == "/api/timetable":
                return self._timetable(body)
        except Exception:
            log.exception("web: request failed")
            return self._json(500, {"error": "Something went wrong on the laptop. Check its terminal."})
        self._json(404, {"error": "not found"})

    def _shampoo(self, body: dict) -> None:
        try:
            day = date.fromisoformat(str(body.get("date", "")))
        except ValueError:
            return self._json(400, {"error": "invalid date"})
        on = body.get("on")
        if not isinstance(on, bool):
            return self._json(400, {"error": "'on' must be true or false"})
        self.app.store.update(lambda s: s.set_shampoo(day, on))
        log.info("web: %s set for %s", "shampoo day" if on else "no shampoo", day)
        self._json(200, {"ok": True})

    def _timetable(self, body: dict) -> None:
        path = self.app.cfg.timetable.path
        if path.suffix.lower() != ".csv":
            return self._json(400, {"error": "Only .csv timetables can be edited here."})
        try:
            rows = validate_rows(body.get("rows"))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        write_rows(path, rows)
        log.info("web: timetable saved (%d classes)", len(rows))
        self._json(200, {"ok": True})


class WebServer:
    def __init__(self, app: App):
        self.app = app
        self.key = load_or_create_key(app.cfg.data_dir)
        icons = {"/icon-180.png": make_icon(180), "/icon-512.png": make_icon(512), "/favicon.png": make_icon(32)}
        handler = type("Handler", (_Handler,), {"app": app, "key": self.key, "icons": icons})
        self.httpd = ThreadingHTTPServer((app.cfg.web.host, app.cfg.web.port), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def links(self) -> list[str]:
        return links(self.key, self.port)

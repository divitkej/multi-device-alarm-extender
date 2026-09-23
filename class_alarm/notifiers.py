"""Phone alert channels: ntfy push, Pushover emergency push, Twilio phone call.

A failing channel only logs a warning. It must never stop the laptop alarm.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.sax.saxutils import escape

from class_alarm.config import (
    BehaviorConfig,
    NativeAlarmConfig,
    NtfyConfig,
    PhoneConfig,
    PushoverConfig,
    TwilioConfig,
)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


@dataclass
class Request:
    url: str
    data: bytes
    headers: dict[str, str]


def send(req: Request) -> bytes:
    r = urllib.request.Request(req.url, data=req.data, headers=req.headers, method="POST")
    with urllib.request.urlopen(r, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def get(url: str, headers: dict[str, str]) -> bytes:
    r = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(r, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        return f"HTTP {exc.code}: {body}".strip()
    return str(exc)


class Notifier:
    """Base class. `interval` is how often to re-alert while the alarm is ringing (None = once)."""

    name = "notifier"

    def __init__(self, interval: float | None, sender=send):
        self.interval = interval
        self._send = sender
        self._last: float | None = None

    def build(self, message: str) -> Request:
        raise NotImplementedError

    def _fire(self, message: str) -> None:
        try:
            self.handle_response(self._send(self.build(message)))
            log.info("%s: alert sent", self.name)
        except Exception as exc:  # network down, bad credentials, etc.
            log.warning("%s: failed to alert phone: %s", self.name, _describe_error(exc))

    def handle_response(self, body: bytes) -> None:
        pass

    def start(self, message: str, now: float | None = None) -> None:
        self._last = time.monotonic() if now is None else now
        self._fire(message)

    def tick(self, message: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.interval is None or self._last is None:
            return
        if now - self._last >= self.interval:
            self._last = now
            self._fire(message)

    def stop(self) -> None:
        self._last = None


class NtfyNotifier(Notifier):
    """Free push via the ntfy app (Android works best). Priority 5 = max/urgent."""

    name = "ntfy"

    def __init__(self, cfg: NtfyConfig, interval: float, sender=send):
        super().__init__(interval, sender)
        self.cfg = cfg

    def build(self, message: str) -> Request:
        headers = {
            "Title": "Class Alarm",
            "Priority": "5",
            "Tags": "alarm_clock",
        }
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        url = f"{self.cfg.server}/{urllib.parse.quote(self.cfg.topic, safe='')}"
        return Request(url=url, data=message.encode("utf-8"), headers=headers)


class PushoverNotifier(Notifier):
    """Pushover emergency priority: the phone re-alerts every retry_seconds until you acknowledge.

    Pushover does the repeating itself, so this sends once and cancels on dismiss.
    """

    name = "pushover"
    API = "https://api.pushover.net/1"
    EXPIRE_SECONDS = 1800

    def __init__(self, cfg: PushoverConfig, sender=send):
        super().__init__(None, sender)
        self.cfg = cfg
        self.receipt: str | None = None

    def build(self, message: str) -> Request:
        fields = {
            "token": self.cfg.app_token,
            "user": self.cfg.user_key,
            "title": "Class Alarm",
            "message": message,
            "priority": "2",
            "retry": str(self.cfg.retry_seconds),
            "expire": str(self.EXPIRE_SECONDS),
            "sound": self.cfg.sound,
        }
        return Request(
            url=f"{self.API}/messages.json",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def handle_response(self, body: bytes) -> None:
        try:
            self.receipt = json.loads(body).get("receipt")
        except (ValueError, AttributeError):
            self.receipt = None

    def stop(self) -> None:
        super().stop()
        if not self.receipt:
            return
        receipt, self.receipt = self.receipt, None
        req = Request(
            url=f"{self.API}/receipts/{urllib.parse.quote(receipt, safe='')}/cancel.json",
            data=urllib.parse.urlencode({"token": self.cfg.app_token}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            self._send(req)
        except Exception as exc:
            log.warning("pushover: could not cancel repeating alert: %s", _describe_error(exc))


class TwilioCallNotifier(Notifier):
    """Places a real phone call that reads the wake-up message aloud."""

    name = "twilio"

    def __init__(self, cfg: TwilioConfig, sender=send):
        super().__init__(cfg.call_every_minutes * 60, sender)
        self.cfg = cfg

    def build(self, message: str) -> Request:
        twiml = f'<Response><Say loop="3">{escape(message)}</Say></Response>'
        fields = {"To": self.cfg.to_number, "From": self.cfg.from_number, "Twiml": twiml}
        auth = base64.b64encode(f"{self.cfg.account_sid}:{self.cfg.auth_token}".encode()).decode()
        sid = urllib.parse.quote(self.cfg.account_sid, safe="")
        return Request(
            url=f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
            data=urllib.parse.urlencode(fields).encode(),
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )


def publish_native_alarm(cfg: NativeAlarmConfig, text: str, sender=send) -> bool:
    """Posts the wake time ("06:45" or "none") for the phone shortcut to read. True on success."""
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    url = f"{cfg.server}/{urllib.parse.quote(cfg.topic, safe='')}"
    try:
        sender(Request(url=url, data=text.encode("utf-8"), headers=headers))
    except Exception as exc:
        log.warning("native alarm: could not publish wake time: %s", _describe_error(exc))
        return False
    return True


def build_notifiers(cfg: PhoneConfig, sender=send) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if cfg.ntfy.enabled:
        notifiers.append(NtfyNotifier(cfg.ntfy, cfg.repeat_seconds, sender))
    if cfg.pushover.enabled:
        notifiers.append(PushoverNotifier(cfg.pushover, sender))
    if cfg.twilio.enabled:
        notifiers.append(TwilioCallNotifier(cfg.twilio, sender))
    return notifiers


class Messenger:
    """Non-alarm phone messages: bedtime reminders and the "are you awake?" check.

    Uses ntfy and/or Pushover (whichever are enabled). Twilio is only for the alarm itself.
    """

    PUSHOVER_API = PushoverNotifier.API

    def __init__(self, phone: PhoneConfig, behavior: BehaviorConfig, sender=send, getter=get):
        self.phone = phone
        self.behavior = behavior
        self._send = sender
        self._get = getter
        self.receipt: str | None = None

    def _post(self, name: str, req: Request) -> bytes | None:
        try:
            return self._send(req)
        except Exception as exc:
            log.warning("%s: failed to message phone: %s", name, _describe_error(exc))
            return None

    def _ntfy(self, title: str, message: str, priority: str, actions: list | None = None) -> Request:
        cfg = self.phone.ntfy
        headers = {"Title": title, "Priority": priority}
        if cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        if actions:
            headers["Actions"] = json.dumps(actions)
        url = f"{cfg.server}/{urllib.parse.quote(cfg.topic, safe='')}"
        return Request(url=url, data=message.encode("utf-8"), headers=headers)

    def _pushover(self, title: str, message: str, **extra: str) -> Request:
        cfg = self.phone.pushover
        fields = {"token": cfg.app_token, "user": cfg.user_key, "title": title, "message": message, **extra}
        return Request(
            url=f"{self.PUSHOVER_API}/messages.json",
            data=urllib.parse.urlencode(fields).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def info(self, title: str, message: str) -> None:
        if self.phone.ntfy.enabled:
            self._post("ntfy", self._ntfy(title, message, "4"))
        if self.phone.pushover.enabled:
            self._post("pushover", self._pushover(title, message, priority="0"))

    def start_awake_check(self, message: str, window_seconds: int) -> None:
        """Pushover: repeating emergency alert we can see acknowledged.
        ntfy: an "I'm awake" button that publishes `awake` to the behavior events topic."""
        if self.phone.ntfy.enabled:
            actions = None
            if self.behavior.enabled:
                events_url = f"{self.behavior.server}/{urllib.parse.quote(self.behavior.events_topic, safe='')}"
                action = {"action": "http", "label": "I'm awake", "url": events_url, "method": "POST", "body": "awake", "clear": True}
                if self.behavior.token:
                    action["headers"] = {"Authorization": f"Bearer {self.behavior.token}"}
                actions = [action]
            self._post("ntfy", self._ntfy("Are you awake?", message, "5", actions))
        if self.phone.pushover.enabled:
            body = self._post(
                "pushover",
                self._pushover(
                    "Are you awake?",
                    message,
                    priority="2",
                    retry="60",
                    expire=str(max(window_seconds + 120, 120)),
                    sound="pushover",
                ),
            )
            try:
                self.receipt = json.loads(body).get("receipt") if body else None
            except (ValueError, AttributeError):
                self.receipt = None

    def awake_acknowledged(self) -> bool:
        if not self.receipt:
            return False
        token = urllib.parse.quote(self.phone.pushover.app_token, safe="")
        url = f"{self.PUSHOVER_API}/receipts/{urllib.parse.quote(self.receipt, safe='')}.json?token={token}"
        try:
            return json.loads(self._get(url, {})).get("acknowledged") == 1
        except Exception as exc:
            log.warning("pushover: could not check acknowledgement: %s", _describe_error(exc))
            return False

    def stop_awake_check(self) -> None:
        if not self.receipt:
            return
        receipt, self.receipt = self.receipt, None
        self._post(
            "pushover",
            Request(
                url=f"{self.PUSHOVER_API}/receipts/{urllib.parse.quote(receipt, safe='')}/cancel.json",
                data=urllib.parse.urlencode({"token": self.phone.pushover.app_token}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
        )

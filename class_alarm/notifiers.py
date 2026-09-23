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

from class_alarm.config import NtfyConfig, PhoneConfig, PushoverConfig, TwilioConfig

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


def build_notifiers(cfg: PhoneConfig, sender=send) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if cfg.ntfy.enabled:
        notifiers.append(NtfyNotifier(cfg.ntfy, cfg.repeat_seconds, sender))
    if cfg.pushover.enabled:
        notifiers.append(PushoverNotifier(cfg.pushover, sender))
    if cfg.twilio.enabled:
        notifiers.append(TwilioCallNotifier(cfg.twilio, sender))
    return notifiers

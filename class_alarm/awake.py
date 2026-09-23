"""After the alarm is turned off: make sure you didn't go back to sleep."""

from __future__ import annotations

import logging
import time
from typing import Callable

from class_alarm.behavior import awake_signal_since
from class_alarm.config import AwakeCheckConfig
from class_alarm.notifiers import Messenger
from class_alarm.state import Event

log = logging.getLogger(__name__)

POLL_SECONDS = 15
CLOCK_SKEW_SECONDS = 5  # ntfy stamps events with its own clock


def confirm_awake(
    cfg: AwakeCheckConfig,
    messenger: Messenger,
    poll_events: Callable[[], list[Event]],
    wall: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
    on_check_sent: Callable[[], None] | None = None,
) -> bool:
    """Waits check_after_minutes, then asks the phone "are you awake?".

    Awake if, within confirm_window_minutes, you acknowledge it (Pushover), tap "I'm awake"
    (ntfy), or the phone reports any activity. Returns False if nothing happens.
    """
    check_at = wall() + cfg.check_after_minutes * 60
    while wall() < check_at:
        poll_events()  # keep the log current; activity before the check doesn't count
        sleep(min(POLL_SECONDS, max(0.0, check_at - wall())))

    window = cfg.confirm_window_minutes * 60
    sent_at = wall()
    messenger.start_awake_check(
        f"Alarm is off. Confirm within {cfg.confirm_window_minutes} min or the laptop and phone ring again.",
        window,
    )
    out(f"Sent 'are you awake?' to your phone. Waiting {cfg.confirm_window_minutes} min for an answer.")
    if on_check_sent:
        on_check_sent()
    try:
        while wall() < sent_at + window:
            signal = awake_signal_since(poll_events(), sent_at - CLOCK_SKEW_SECONDS)
            if signal:
                log.info("awake: phone reported %s", signal.kind)
                return True
            if messenger.awake_acknowledged():
                log.info("awake: acknowledged on phone")
                return True
            sleep(min(POLL_SECONDS, max(0.0, sent_at + window - wall())))
        return False
    finally:
        messenger.stop_awake_check()

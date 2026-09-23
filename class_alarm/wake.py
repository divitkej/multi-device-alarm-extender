"""Asks the operating system to wake the laptop from sleep shortly before an alarm.

Without this, a sleeping laptop cannot ring. One pending wake is kept at a time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

WINDOWS_TASK = "ClassAlarmWake"


class WakeError(Exception):
    pass


def wake_command(when: datetime, platform: str = sys.platform) -> list[str]:
    local = when.astimezone()
    if platform == "darwin":
        # pmset date format per `man pmset`: "MM/dd/yy HH:mm:ss"
        return ["pmset", "schedule", "wake", local.strftime("%m/%d/%y %H:%M:%S")]
    if platform.startswith("linux"):
        # -m no: only set the RTC alarm, don't suspend now. Replaces any previous RTC alarm.
        return ["rtcwake", "-m", "no", "-t", str(int(local.timestamp()))]
    if platform == "win32":
        script = (
            f"$t = New-ScheduledTaskTrigger -Once -At '{local:%Y-%m-%dT%H:%M:%S}'; "
            "$s = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries; "
            "$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c exit'; "
            f"Register-ScheduledTask -TaskName '{WINDOWS_TASK}' -Trigger $t -Settings $s "
            "-Action $a -Force | Out-Null"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    raise WakeError(f"waking from sleep is not supported on {platform}")


def schedule_wake(when: datetime) -> list[str]:
    """Runs the OS command. On macOS/Linux this needs root, so non-root runs use `sudo -n`."""
    cmd = wake_command(when)
    if sys.platform != "win32" and hasattr(os, "geteuid") and os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WakeError(f"could not run {cmd[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WakeError(f"{' '.join(cmd[:3])} failed: {detail or 'exit code ' + str(result.returncode)}")
    return cmd

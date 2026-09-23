# Class Alarm

Reads your college timetable, works out when to wake you (45 minutes before your first class of the day by default), and rings your laptop and your phone at the same time.

- 7:30 class: alarm at 6:45
- 9:00 class: alarm at 8:15
- No class that day: no alarm

## How it works

1. You give it your timetable as a weekly CSV or a `.ics` calendar export.
2. For each day it finds the earliest class and sets an alarm `lead_minutes` before it.
3. At alarm time the laptop plays a loud looping beep and your phone gets alerts (push, repeating push, or a real phone call).
4. Everything keeps ringing until you type a random 4 digit code shown on the laptop screen. You have to get up and walk to the laptop, which is the point.

## Requirements

- Python 3.11 or newer (Windows, macOS or Linux)
- For `.ics` timetables: `pip install ".[ics]"`

## Setup

```bash
git clone https://github.com/divitkej/multi-device-alarm-extender.git
cd multi-device-alarm-extender
pip install .            # or: pip install ".[ics]" for .ics timetables
cp config.example.toml config.toml
cp timetable.example.csv timetable.csv
```

Then edit `timetable.csv` and `config.toml`.

### 1. Your timetable

**Option A: CSV** (works for any college). One row per class, repeating weekly:

```csv
day,start,end,course,location
Mon/Wed/Fri,07:30,08:45,MATH 101,Room 204
Tue/Thu,09:00,10:15,ENGL 102,Library 12
```

- `day`: `Mon`, `Tue`, `Wed`, `Thu`, `Fri`, `Sat`, `Sun`, full names, or `weekdays`. Combine with `/` or spaces.
- `start`: `07:30`, `7:30 AM` and `19:30` all work.
- `end`, `course`, `location` are optional.

**Option B: `.ics` calendar.** If your college portal, Google Calendar or Outlook can export your schedule as `.ics`, point `path` at that file. One-off changes, cancelled classes and excluded dates in the calendar are respected. All-day events (holidays) are ignored.

**If your timetable is a PDF or a screenshot**, copy it into the CSV format above. It takes a few minutes and you only do it once per semester.

Useful options in `[timetable]`:

- `lead_minutes = 45`: how long before class to wake you
- `term_start`, `term_end`: no alarms outside the semester
- `skip_dates = [2026-11-26]`: holidays and days off
- `ignore_keywords = ["office hours"]`: never wake up for these

Check the result:

```bash
class-alarm plan
```

### 2. Your phone

Pick at least one. You can enable several.

| Channel | Cost | Best for | How loud |
|---|---|---|---|
| ntfy | Free | Android | Max priority push, re-sent every `repeat_seconds` |
| Pushover | One time app purchase | iPhone | Emergency alert that repeats until you acknowledge it, can bypass silent mode |
| Twilio | A few cents per call | Heavy sleepers | Real phone call reading your message aloud, repeated every few minutes |

**ntfy (free)**

1. Install the ntfy app on your phone.
2. Subscribe to a topic. Use a long random name, since anyone who knows it can read your messages.
3. Put the same name in `[phone.ntfy] topic`.
4. Android: in ntfy settings, allow it to override Do Not Disturb and turn on repeated alerting for max priority messages.

**Pushover (recommended for iPhone)**

1. Install Pushover and create an account. Copy your User Key.
2. Create an application at pushover.net to get an API token.
3. Fill in `app_token` and `user_key` and set `enabled = true`.
4. On iPhone, allow Critical Alerts for Pushover so emergency alerts ring even on silent.
5. When you dismiss the alarm on the laptop, the repeating alert on your phone is cancelled automatically.

**Twilio (phone call)**

1. Create a Twilio account and buy a phone number.
2. Fill in `account_sid`, `auth_token`, `from_number` (your Twilio number) and `to_number` (your phone), in `+15551234567` format.
3. Save the Twilio number as a contact and allow it through Do Not Disturb (iPhone: Emergency Bypass on the contact; Android: add it to DND exceptions).
4. Trial accounts can only call verified numbers and play a short trial message first.

Test it:

```bash
class-alarm test-phone     # one alert per enabled channel
class-alarm test           # full alarm right now, laptop and phone
```

### 3. Run it

```bash
class-alarm run
```

Leave that terminal window open. It re-reads your timetable every 15 seconds, so edits take effect without restarting. Start it before bed each night (or keep it running all semester).

When the alarm rings:

- Type the 4 digit code shown and press Enter to stop it
- Type `s` and press Enter to snooze (`snooze_minutes`, up to `max_snoozes` times)
- It stops by itself after `ring_limit_minutes`

## Waking from sleep (important)

A sleeping laptop cannot ring. Pick one:

- **Simplest:** keep the laptop plugged in and set it to never sleep overnight.
- **Let it sleep:** set `os_wake = true`. Before each alarm the program asks the OS to wake the laptop 2 minutes early.
  - **Windows:** creates a scheduled task called `ClassAlarmWake` with "Wake the computer to run this task". Also enable Control Panel > Power Options > Change plan settings > Advanced > Sleep > Allow wake timers.
  - **macOS:** uses `pmset schedule wake`, which needs root. Allow it without a password by running `sudo visudo` and adding `yourusername ALL=(root) NOPASSWD: /usr/bin/pmset`.
  - **Linux:** uses `rtcwake`, which needs root. Add `yourusername ALL=(root) NOPASSWD: /usr/sbin/rtcwake` via `sudo visudo` (check the path with `which rtcwake`).
  - Test with `class-alarm wake-next`.
- Most laptops will not wake from sleep with the lid closed and no external display. Leave the lid open.
- If the laptop wakes late but before class starts, the alarm still rings immediately.

## Troubleshooting

- **No sound on Linux:** install `pulseaudio-utils` (for `paplay`) or `alsa-utils` (for `aplay`).
- **Custom sound:** set `sound_file` to a `.wav` file. WAV works on every OS.
- **Phone alert failed:** the error is printed in the terminal. The laptop keeps ringing regardless.
- **Wrong wake time:** run `class-alarm plan` and check your CSV times and `lead_minutes`.

## Development

```bash
pip install ".[dev]"
pytest
```

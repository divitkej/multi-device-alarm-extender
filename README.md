# Class Alarm

Reads your college timetable, works out when to wake you (45 minutes before your first class of the day by default), and rings your laptop and your phone at the same time.

- 7:30 class: alarm at 6:45
- 7:30 class on a shampoo day: alarm at 6:15 (75 minutes)
- 9:00 class: alarm at 8:15
- No class that day: no alarm

Optional extras that use your phone's behavior:

- **Bedtime reminders:** tells you when to go to sleep for tomorrow's alarm, warns you if your usual phone-down time would leave you short, and nudges you if you're still on your phone after bedtime.
- **Back-to-sleep check:** after you turn the alarm off, your phone asks "are you awake?". No tap and no phone activity means the laptop and phone ring again.

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

### Shampoo days

Shampoo days use `[shampoo] lead_minutes` (75 by default) instead of 45.

- **Every week:** `days = ["Mon", "Thu"]` in `[shampoo]`
- **One-off from the laptop:**
  - `class-alarm shampoo` (next morning with class)
  - `class-alarm shampoo tomorrow`
  - `class-alarm shampoo 2026-09-24`
  - `class-alarm shampoo --off` (skip a weekly shampoo day)
- **One-off from your phone:** a "Shampoo" shortcut (see Phone behavior setup below). Your phone gets a confirmation with the new alarm time.

### 2. Your phone

Pick at least one. You can enable several.

| Channel | Cost | Best for | How loud |
|---|---|---|---|
| Phone's own alarm | Free | Everyone, especially iPhone | The real Clock alarm, rings on silent and through Do Not Disturb |
| ntfy | Free | Android | Max priority push, re-sent every `repeat_seconds` |
| Pushover | One time app purchase | iPhone | Emergency alert that repeats until you acknowledge it, can bypass silent mode |
| Twilio | A few cents per call | Heavy sleepers | Real phone call reading your message aloud, repeated every few minutes |

**Phone's own alarm (free, recommended, works in the UAE)**

Your phone's built-in Clock alarm is the loudest, most reliable thing it has: it rings on silent, through Do Not Disturb and with the phone locked. With `[phone.native_alarm]` on, the laptop publishes tomorrow's wake time (for example `06:45`, or `none` when there's no class in the next 24 hours) and a phone shortcut turns it into a real alarm every night. It keeps working even if the laptop is asleep in the morning.

iPhone setup (Shortcuts app):

1. New shortcut named **Set Class Alarm** with these actions:
   1. **Get Contents of URL:** `https://ntfy.sh/YOUR-NATIVE-ALARM-TOPIC/raw?poll=1`
   2. **Split Text** by New Lines
   3. **Get Item from List:** Last Item
   4. **If** Item is `none`: **Stop This Shortcut**
   5. **Get Dates from Input** (the Item)
   6. **Create Alarm** at that date, label `Class`
2. Automation > New Automation > **Time of Day** (for example 10:00 PM, daily) > **Run Immediately** > run **Set Class Alarm**. Add a second automation on **Charger > Is Connected** if you often go to bed earlier or later.
3. Test with `class-alarm test-phone`, then run the shortcut and check the Clock app.

Notes:

- The laptop re-publishes every hour and whenever the time changes (shampoo day, timetable edit). ntfy.sh keeps messages for 12 hours, so the laptop must have been awake at some point in the 12 hours before the shortcut runs.
- Each night adds a new one-time alarm. iOS turns old ones off after they ring, but they stay in the list, so delete them now and then.
- Android: MacroDroid or Tasker can read the same URL and set an alarm.

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

## Phone app

The laptop serves a small app you open on your phone and keep on your Home Screen. It works on iPhone and Android, needs no App Store and no Mac.

What it shows and does:

- Next alarm, with a **Shampoo day** checkbox
- This week's alarms, each with its own shampoo checkbox
- An alert while the laptop is ringing
- A big **I'm awake** button during the back-to-sleep check
- Tonight's bedtime, your usual phone-down time and the last 7 nights
- A timetable editor (for `.csv` timetables). It checks your entries before saving and keeps the previous file as `timetable.csv.bak`

Setup:

1. In `config.toml` set `[web] enabled = true`.
2. Run `class-alarm run`. It prints a private link like `http://192.168.1.20:8765/?key=...`.
3. Open that link on your phone while it's on the **same Wi-Fi** as the laptop.
4. iPhone: Share > **Add to Home Screen**. Android: menu > **Add to Home screen**.
5. Lost the link? `class-alarm web-link` prints it again.

Good to know:

- It only works while `class-alarm run` is running and your phone is on the same Wi-Fi. Away from home it shows "Can't reach your laptop".
- The first time, Windows asks whether to allow Python through the firewall. Allow it on **private** networks.
- If your router gives the laptop a new address, the Home Screen icon stops working. Either use the `.local` link that is also printed, or reserve the laptop's address in your router settings.
- The link contains a secret key. Anyone with the link on your Wi-Fi can use the app, so don't share it. To change the key, delete `data/web_key` and restart.
- The connection is plain HTTP inside your Wi-Fi. Use it on your home Wi-Fi, not on public or campus Wi-Fi.
- The back-to-sleep check can use the app's **I'm awake** button, so `[awake_check]` works with just ntfy + `[web]`, without setting up behavior tracking.

## Phone behavior setup (bedtime reminders and back-to-sleep check)

Your phone tells the laptop what it's doing by sending one word to a **second** private ntfy topic (`[behavior] events_topic`). The laptop reads that topic every 30 seconds. You don't need to subscribe to it in the ntfy app, and no ntfy app is needed for this part.

Words it understands: `app_open`, `app_close`, `unlock`, `charger_on`, `charger_off`, `sleep_focus_on`, `sleep_focus_off`, `awake`, `shampoo`, `no_shampoo`.

Each word is sent by opening this URL (replace the topic):

```
https://ntfy.sh/YOUR-EVENTS-TOPIC/publish?message=app_open
```

### iPhone (Shortcuts app, iOS 17 or newer)

Create these in Shortcuts > Automation > New Automation. For each one pick **Run Immediately**, then add a single **Get Contents of URL** action with the URL above and the matching word.

| Trigger | Word |
|---|---|
| App > pick the apps you use most at night and in the morning (Instagram, TikTok, YouTube, WhatsApp, Safari...) > Is Opened | `app_open` |
| Same apps > Is Closed | `app_close` |
| Charger > Is Connected | `charger_on` |
| Charger > Is Disconnected | `charger_off` |
| Focus > Sleep > When Turning On | `sleep_focus_on` |
| Focus > Sleep > When Turning Off | `sleep_focus_off` |

Also create three normal shortcuts (not automations) with the same action, and add them to your Home Screen or to Back Tap (Settings > Accessibility > Touch > Back Tap):

- **I'm awake:** `awake`
- **Shampoo:** `shampoo`
- **No shampoo:** `no_shampoo`

Check that events arrive with `class-alarm events`.

**What iOS does not allow:** third-party apps and Shortcuts cannot see screen unlocks or Screen Time data. App opens, charging and Sleep Focus are the closest signals iOS gives, so choose the apps you actually open in bed.

### Android

Use MacroDroid, Tasker or Automate with an HTTP request action and the same URL. Android can also detect screen unlocks, so add an `unlock` trigger. The ntfy app's **I'm awake** button works on Android too.

### Bedtime reminders (`[sleep]`)

- Bedtime = tomorrow's alarm time minus `sleep_hours` minus `fall_asleep_minutes` (6:45 alarm, 7.5h, 15 min: bed by 11:00 PM).
- `wind_down_minutes` before bedtime you get a heads up. After a few nights of data, it also tells you when you usually put your phone down and how much sleep that would leave.
- At bedtime: "Time to sleep".
- If your phone is still being used after bedtime, you get a nudge every `nag_minutes` (up to `max_nags`).
- `class-alarm sleep-report` shows the last week of nights and tonight's recommendation.
- Your sleep pattern is estimated from the longest stretch with no phone activity each night (at least 3 hours, ended by a morning event). It measures when you put the phone down, not true sleep.

### Back-to-sleep check (`[awake_check]`)

1. You turn the alarm off on the laptop.
2. After `check_after_minutes`, your phone gets "Are you awake?".
3. You're counted as awake if, within `confirm_window_minutes`, you do any of these:
   - acknowledge the Pushover alert
   - tap **I'm awake** on the ntfy notification (Android)
   - run the I'm awake shortcut
   - use your phone in a way it reports (opening a tracked app, unplugging the charger)
4. Otherwise the laptop and phone ring again, up to `max_rerings` times.

Plugging in the charger or turning on Sleep Focus does not count as awake.

On iPhone, use Pushover for this check: its alerts can be acknowledged from the lock screen and the laptop can see that you did.

## Waking from sleep (important)

A sleeping laptop cannot ring. Pick one:

- **Simplest:** keep the laptop plugged in and set it to never sleep overnight.
- **Let it sleep:** set `os_wake = true`. Before each alarm the program asks the OS to wake the laptop 2 minutes early.
  - **Windows:** creates a scheduled task called `ClassAlarmWake` with "Wake the computer to run this task". Also enable Control Panel > Power Options > Change plan settings > Advanced > Sleep > Allow wake timers.
  - **macOS:** uses `pmset schedule wake`, which needs root. Allow it without a password by running `sudo visudo` and adding `yourusername ALL=(root) NOPASSWD: /usr/bin/pmset`.
  - **Linux:** uses `rtcwake`, which needs root. Add `yourusername ALL=(root) NOPASSWD: /usr/sbin/rtcwake` via `sudo visudo` (check the path with `which rtcwake`).
  - Test with `class-alarm wake-next`.
- With `[sleep]` on, it also wakes the laptop before your wind-down reminder so the reminder can be sent.
- Most laptops will not wake from sleep with the lid closed and no external display. Leave the lid open.
- If the laptop wakes late but before class starts, the alarm still rings immediately.

## Troubleshooting

- **No sound on Linux:** install `pulseaudio-utils` (for `paplay`) or `alsa-utils` (for `aplay`).
- **Custom sound:** set `sound_file` to a `.wav` file. WAV works on every OS.
- **Phone alert failed:** the error is printed in the terminal. The laptop keeps ringing regardless.
- **Wrong wake time:** run `class-alarm plan` and check your CSV times, `lead_minutes` and shampoo days.
- **No phone events:** run `class-alarm events`. If nothing shows, open the URL from your phone's browser once to check the topic name, then check the automations are set to Run Immediately.

## Privacy

- Phone events contain only a word (like `app_open`) and a time, not which app or anything you did in it.
- On the public ntfy.sh server, anyone who knows a topic name can read it, so use long random topic names. For more privacy, run your own ntfy server or use ntfy access tokens (`token` in config).
- The learned data stays on your laptop in the `data/` folder (git-ignored). Events older than 30 days are deleted.

## Development

```bash
pip install ".[dev]"
pytest
```

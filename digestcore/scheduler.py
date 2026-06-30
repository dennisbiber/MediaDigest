"""5-field cron matching and a lightweight minute-resolution scheduler.

The scheduler ticks in UTC; the pipeline converts ``now`` into each subscription's
timezone before matching, so a user's cron times mean what they expect in their own
local time regardless of the container clock.
"""

import threading
import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def safe_zone(tz: str) -> dt.tzinfo:
    """Resolve a timezone name, falling back to UTC for blank/invalid values.

    A blank tz is a legitimate 'use UTC' default and falls back quietly. A *non-blank*
    tz that won't resolve is a real problem — most often the container is missing the
    timezone database (install the 'tzdata' package) — so it warns rather than silently
    shifting everyone's schedule to UTC."""
    if not tz:
        return dt.timezone.utc
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"[scheduler] WARNING: timezone '{tz}' could not be resolved (is the "
              "'tzdata' package installed?); falling back to UTC — cron times will be "
              "off.", flush=True)
        return dt.timezone.utc


def _field_match(field_str: str, value: int, names=None) -> bool:
    if field_str.strip() == "*":
        return True
    for part in field_str.split(","):
        part = part.strip().lower()
        if names and part in names and names[part] == value:
            return True
        if part.isdigit() and int(part) == value:
            return True
    return False


def cron_match(cron: str, now: dt.datetime) -> bool:
    """Match a 5-field cron (minute hour day-of-month month day-of-week) against a
    timezone-aware ``now`` already localized to the subscriber's timezone."""
    try:
        minute, hour, dom, month, dow = cron.split()
    except (ValueError, AttributeError):
        return False
    return (_field_match(minute, now.minute) and _field_match(hour, now.hour)
            and _field_match(dom, now.day) and _field_match(month, now.month)
            and _field_match(dow, now.weekday(), _DOW))


class MinuteScheduler:
    def __init__(self, tick_fn):
        self._tick_fn = tick_fn
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        last = None
        while not self._stop.wait(20):
            now = dt.datetime.now(dt.timezone.utc)
            key = now.strftime("%Y%m%d%H%M")
            if key == last:
                continue
            last = key
            try:
                self._tick_fn(now)
            except Exception as e:
                print(f"digest tick error: {e}")
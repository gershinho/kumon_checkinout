"""Time handling for the center: local timezone, 12-hour display formatting,
and the center's daily close time.

Timestamps are stored in the database as naive ISO strings in the center's
local time (e.g. "2026-08-10T16:05:00"). Everything that needs "now" should go
through local_now() so the app agrees with the wall clock at the center even if
the server's own clock is set to a different timezone.
"""
import os
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Kumon runs Monday and Thursday, 2 PM - 10 PM.
DEFAULT_CLOSE_HOUR = 22
DEFAULT_CLOSE_MINUTE = 0
DEFAULT_REPORT_DAYS = "mon,thu"

_WEEKDAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def get_timezone():
    """The center's timezone from REPORT_TIMEZONE, or None to use system local time."""
    name = (os.environ.get("REPORT_TIMEZONE") or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_now() -> datetime:
    """Current wall-clock time at the center, as a naive datetime."""
    tz = get_timezone()
    now = datetime.now(tz) if tz else datetime.now()
    return now.replace(tzinfo=None)


def local_today():
    return local_now().date()


def close_time() -> time:
    """The time the center closes and the day resets (default 10:00 PM)."""
    try:
        hour = int(os.environ.get("CENTER_CLOSE_HOUR", DEFAULT_CLOSE_HOUR))
        minute = int(os.environ.get("CENTER_CLOSE_MINUTE", DEFAULT_CLOSE_MINUTE))
        return time(hour, minute)
    except (TypeError, ValueError):
        return time(DEFAULT_CLOSE_HOUR, DEFAULT_CLOSE_MINUTE)


def report_days() -> list[str]:
    """Weekdays a PDF report is generated on, as lowercase abbreviations."""
    raw = os.environ.get("REPORT_DAYS", DEFAULT_REPORT_DAYS)
    days = [d.strip().lower()[:3] for d in raw.split(",") if d.strip()]
    valid = [d for d in days if d in _WEEKDAY_ABBR]
    return valid or DEFAULT_REPORT_DAYS.split(",")


def is_report_day(day) -> bool:
    return _WEEKDAY_ABBR[day.weekday()] in report_days()


def _coerce(value):
    """Accept an ISO string, a datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def fmt_time(value, empty="—") -> str:
    """4:05 PM  (built by hand rather than strftime('%-I') so it also works on Windows)"""
    dt = _coerce(value)
    if dt is None:
        return empty
    hour = dt.hour % 12 or 12
    meridiem = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {meridiem}"


def fmt_date(value, empty="—") -> str:
    """Monday, August 10, 2026"""
    dt = _coerce(value) if not hasattr(value, "strftime") else value
    if dt is None:
        return empty
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def fmt_date_short(value, empty="—") -> str:
    """Mon, Aug 10"""
    dt = _coerce(value) if not hasattr(value, "strftime") else value
    if dt is None:
        return empty
    return f"{dt.strftime('%a, %b')} {dt.day}"


def fmt_datetime(value, empty="—") -> str:
    """Mon, Aug 10 at 4:05 PM"""
    dt = _coerce(value)
    if dt is None:
        return empty
    return f"{fmt_date_short(dt)} at {fmt_time(dt)}"

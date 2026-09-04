"""Time string parsing utilities for match scheduling."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_RELATIVE = re.compile(
    r"^(?:in\s+)?(?:(?P<days>\d+)\s*d(?:ays?)?)?\s*"
    r"(?:(?P<hours>\d+)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(?P<minutes>\d+)\s*m(?:in(?:utes?)?)?)?$",
    re.IGNORECASE,
)
_TIME = re.compile(
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?", re.IGNORECASE
)


class TimeParseError(ValueError):
    pass


def get_zone(name: str) -> tzinfo:
    """Resolve an IANA zone name, degrading to UTC rather than raising.

    ``zoneinfo`` has no bundled database on Windows, so a missing ``tzdata``
    package makes even ``ZoneInfo("UTC")`` fail: hence the second fallback.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    try:
        return ZoneInfo("UTC")
    except Exception:
        return timezone.utc


def _time_of_day(text: str) -> tuple[int, int] | None:
    match = _TIME.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = (match.group("ampm") or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise TimeParseError(f"`{text}` is not a valid time of day.")
    return hour, minute


def parse_when(text: str, tz_name: str = "UTC", *, now: datetime | None = None) -> datetime:
    """Parse a user-supplied time into an aware UTC datetime in the future."""
    raw = text.strip()
    if not raw:
        raise TimeParseError("Give me a time, e.g. `tomorrow 20:00` or `in 2h`.")

    zone = get_zone(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(zone)
    lowered = raw.lower()

    # A raw unix timestamp, e.g. pasted from a Discord timestamp.
    if lowered.isdigit() and len(lowered) >= 9:
        return datetime.fromtimestamp(int(lowered), tz=timezone.utc)

    # Relative: "in 2h", "90m", "1d 6h"
    relative = _RELATIVE.match(lowered)
    if relative and any(relative.groupdict().values()):
        delta = timedelta(
            days=int(relative.group("days") or 0),
            hours=int(relative.group("hours") or 0),
            minutes=int(relative.group("minutes") or 0),
        )
        if delta:
            return (now_local + delta).astimezone(timezone.utc)

    # Absolute date: "2026-09-05 20:00"
    date_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", lowered)
    if date_match:
        clock = _time_of_day(lowered[date_match.end():]) or (0, 0)
        local = datetime(
            int(date_match.group(1)),
            int(date_match.group(2)),
            int(date_match.group(3)),
            clock[0],
            clock[1],
            tzinfo=zone,
        )
        return local.astimezone(timezone.utc)

    clock = _time_of_day(lowered)
    if clock is None:
        raise TimeParseError(
            f"I could not read `{raw}`. Try `20:00`, `tomorrow 8pm`, "
            "`friday 19:30`, `in 90m`, or `2026-09-05 20:00`."
        )

    base = now_local.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)

    if "tomorrow" in lowered:
        base += timedelta(days=1)
    elif "today" in lowered or "tonight" in lowered:
        pass
    else:
        for word, index in WEEKDAYS.items():
            if re.search(rf"\b{word}\b", lowered):
                ahead = (index - base.weekday()) % 7
                # A named weekday that has already passed today means next week.
                if ahead == 0 and base <= now_local:
                    ahead = 7
                base += timedelta(days=ahead)
                break
        else:
            # Bare time: the next time that clock reading comes around.
            if base <= now_local:
                base += timedelta(days=1)

    if base <= now_local:
        raise TimeParseError(
            f"`{raw}` resolves to the past ({base:%Y-%m-%d %H:%M}). "
            "Be more specific, e.g. `tomorrow 20:00`."
        )
    return base.astimezone(timezone.utc)

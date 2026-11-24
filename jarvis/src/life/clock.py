"""Local time for the life organiser: one timezone (config `life.timezone`).

Everything is stored in UTC; everything shown to people or agents is in the
user's zone. Repeating reminders step in local wall-clock time, so "every day
at 9:00" stays at 9:00 across daylight-saving changes.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

RECURRENCES = ("none", "daily", "weekdays", "weekly", "monthly")
UTC = timezone.utc


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC string, so text comparison in SQL sorts correctly."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Clock:
    def __init__(self, tz_name: str):
        self.tz_name = tz_name
        self.tz = ZoneInfo(tz_name)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def label_now(self) -> str:
        return f"{self.now():%a %d %b %Y, %H:%M} ({self.tz_name})"

    def today(self) -> date:
        return self.now().date()

    def local(self, dt: datetime | str) -> datetime:
        if isinstance(dt, str):
            dt = from_iso(dt)
        return dt.astimezone(self.tz)

    def parse(self, text: str) -> datetime | None:
        """A date-time from an agent or a form, as an aware UTC datetime.

        Accepts local wall-clock ("2026-11-25T09:00", "2026-11-25 9:00",
        with or without seconds), a bare date (taken as 09:00), or a full
        ISO string with an offset or Z. Returns None if unparseable.
        """
        s = (text or "").strip()
        if not s:
            return None
        s = s.replace(" ", "T", 1)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            s += "T09:00"
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{1,2}):(\d{2})(:\d{2})?", s)
        if m:
            s = f"{m[1]}T{int(m[2]):02d}:{m[3]}{m[4] or ''}"
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt.astimezone(UTC)

    def parse_date(self, text: str) -> str | None:
        """A calendar date "YYYY-MM-DD" (validated), or None."""
        try:
            return date.fromisoformat((text or "").strip()).isoformat()
        except ValueError:
            return None

    def when(self, dt: datetime | str) -> str:
        """Human label relative to today: "Today 18:00", "Tomorrow 09:00",
        "Fri 18:00" (this week), "Fri 28 Nov 18:00" (further out)."""
        t = self.local(dt)
        days = (t.date() - self.today()).days
        hm = f"{t:%H:%M}"
        if days == 0:
            return f"Today {hm}"
        if days == 1:
            return f"Tomorrow {hm}"
        if days == -1:
            return f"Yesterday {hm}"
        if -7 < days < 7:
            return f"{t:%a} {hm}"
        return f"{t:%a %d %b} {hm}"

    def input_value(self, dt: datetime | str) -> str:
        """Value for an <input type="datetime-local"> in the user's zone."""
        return f"{self.local(dt):%Y-%m-%dT%H:%M}"

    def tomorrow_morning(self) -> datetime:
        t = (self.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        return t.astimezone(UTC)

    def next_occurrence(
        self,
        due: datetime | str,
        recurrence: str,
        after: datetime,
        anchor_day: int | None = None,
    ) -> datetime | None:
        """Next occurrence strictly after `after`. Missed occurrences (the Mac
        was off) are skipped, not replayed. Monthly repeats keep their
        original day (31st -> 28 Feb -> 31 Mar), via `anchor_day`."""
        if recurrence not in RECURRENCES or recurrence == "none":
            return None
        wall = self.local(due).replace(tzinfo=None)
        day = anchor_day or wall.day

        def aware(w: datetime) -> datetime:
            return w.replace(tzinfo=self.tz).astimezone(UTC)

        for _ in range(5000):
            if aware(wall) > after:
                break
            if recurrence == "daily":
                wall += timedelta(days=1)
            elif recurrence == "weekdays":
                wall += timedelta(days=1)
                while wall.weekday() >= 5:
                    wall += timedelta(days=1)
            elif recurrence == "weekly":
                wall += timedelta(weeks=1)
            elif recurrence == "monthly":
                year, month = (wall.year + 1, 1) if wall.month == 12 else (wall.year, wall.month + 1)
                wall = wall.replace(year=year, month=month, day=min(day, calendar.monthrange(year, month)[1]))
        return aware(wall)

    def last_slot(self, hh_mm: str, days: list[int] | None) -> datetime:
        """The most recent local time at hh:mm on one of `days` (0=Mon..6=Sun;
        None = every day) that is <= now, as aware UTC."""
        hour, minute = (int(x) for x in hh_mm.split(":"))
        now = self.now()
        for back in range(8):
            d = now.date() - timedelta(days=back)
            if days is not None and d.weekday() not in days:
                continue
            slot = datetime(d.year, d.month, d.day, hour, minute, tzinfo=self.tz)
            if slot <= now:
                return slot.astimezone(UTC)
        return datetime.min.replace(tzinfo=UTC)

    def next_slot(self, hh_mm: str, days: list[int] | None) -> datetime:
        hour, minute = (int(x) for x in hh_mm.split(":"))
        now = self.now()
        for ahead in range(8):
            d = now.date() + timedelta(days=ahead)
            if days is not None and d.weekday() not in days:
                continue
            slot = datetime(d.year, d.month, d.day, hour, minute, tzinfo=self.tz)
            if slot > now:
                return slot.astimezone(UTC)
        return datetime.max.replace(tzinfo=UTC)


_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_days(spec) -> list[int] | None:
    """'daily' -> None; 'weekdays' -> Mon-Fri; ['sun'] / 'sun' -> [6]."""
    if spec in (None, "", "daily", "every day"):
        return None
    if spec == "weekdays":
        return [0, 1, 2, 3, 4]
    items = spec if isinstance(spec, list) else [s.strip() for s in str(spec).split(",")]
    days = sorted({_DAY_NAMES[str(x).lower()[:3]] for x in items})
    if not days:
        raise ValueError(f"no days in {spec!r}")
    return days

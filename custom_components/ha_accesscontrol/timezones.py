"""Timezone helpers for keeping a controller clock correct.

The controllers store a plain wall clock: no timezone, and no daylight-saving
rules of their own (function 0x30 writes seven BCD bytes and nothing else). So
whenever the local UTC offset changes, the clock silently becomes wrong by the
size of the change, and every record it timestamps afterwards is wrong too.

Home Assistant therefore has to rewrite the clock at each transition, which
means knowing when the next one is due.

Standard library only, so the protocol test suite can exercise it without
Home Assistant installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

# Long enough to cover any annual rule, short enough for the scan to stay cheap.
SEARCH_HORIZON = timedelta(days=400)

_SCAN_STEP = timedelta(days=1)
_RESOLUTION = timedelta(minutes=1)


def utc_offset(tz: tzinfo, moment: datetime) -> timedelta:
    """Return the UTC offset of ``tz`` at ``moment`` (an aware datetime)."""
    offset = moment.astimezone(tz).utcoffset()
    return offset if offset is not None else timedelta()


def is_dst(tz: tzinfo, moment: datetime) -> bool:
    """Whether daylight saving time is in effect in ``tz`` at ``moment``."""
    return bool(moment.astimezone(tz).dst())


def next_utc_offset_change(
    tz: tzinfo, after: datetime, *, horizon: timedelta = SEARCH_HORIZON
) -> datetime | None:
    """First instant after ``after`` at which the UTC offset of ``tz`` differs.

    Returns an aware UTC datetime, or ``None`` when no change is due within
    ``horizon`` -- a zone without daylight saving, for instance.

    ``zoneinfo`` exposes no transition table, so this walks forward one day at
    a time and then narrows the crossing down to the minute. That is about 365
    offset lookups in the worst case, which is negligible and runs at most
    twice a year per controller.
    """
    if after.tzinfo is None:
        raise ValueError("'after' must be an aware datetime")

    start = after.astimezone(timezone.utc)
    baseline = utc_offset(tz, start)
    end = start + horizon

    probe = start
    while probe < end:
        following = min(probe + _SCAN_STEP, end)
        if utc_offset(tz, following) != baseline:
            low, high = probe, following
            while high - low > _RESOLUTION:
                middle = low + (high - low) / 2
                if utc_offset(tz, middle) == baseline:
                    low = middle
                else:
                    high = middle
            return high
        if following == end:
            break
        probe = following

    return None

"""Daylight-saving helpers used to keep the controller clock correct."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
import uhppote_timezones as tz_helpers

PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
# Asia/Kolkata has a half-hour offset and no daylight saving.
KOLKATA = ZoneInfo("Asia/Kolkata")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --------------------------------------------------------------- offsets


@pytest.mark.parametrize(
    ("zone", "moment", "offset", "dst"),
    [
        # Europe/Paris switches on the last Sunday of March at 01:00 UTC.
        (PARIS, utc(2026, 3, 29, 0, 59), timedelta(hours=1), False),
        (PARIS, utc(2026, 3, 29, 1, 0), timedelta(hours=2), True),
        (PARIS, utc(2026, 10, 25, 0, 59), timedelta(hours=2), True),
        (PARIS, utc(2026, 10, 25, 1, 0), timedelta(hours=1), False),
        (NEW_YORK, utc(2026, 7, 1), timedelta(hours=-4), True),
        (NEW_YORK, utc(2026, 1, 1), timedelta(hours=-5), False),
        (KOLKATA, utc(2026, 7, 1), timedelta(hours=5, minutes=30), False),
        (UTC, utc(2026, 7, 1), timedelta(0), False),
    ],
)
def test_offset_and_dst(
    zone: tzinfo, moment: datetime, offset: timedelta, dst: bool
) -> None:
    assert tz_helpers.utc_offset(zone, moment) == offset
    assert tz_helpers.is_dst(zone, moment) is dst


# ------------------------------------------------------------ transitions


@pytest.mark.parametrize(
    ("zone", "after", "expected"),
    [
        # Spring forward, found from well before and from the previous minute.
        (PARIS, utc(2026, 1, 1), utc(2026, 3, 29, 1, 0)),
        (PARIS, utc(2026, 3, 29, 0, 58), utc(2026, 3, 29, 1, 0)),
        # Fall back.
        (PARIS, utc(2026, 3, 29, 2, 0), utc(2026, 10, 25, 1, 0)),
        # The United States switch on different dates than Europe.
        (NEW_YORK, utc(2026, 1, 1), utc(2026, 3, 8, 7, 0)),
        (NEW_YORK, utc(2026, 4, 1), utc(2026, 11, 1, 6, 0)),
    ],
)
def test_next_transition(zone: tzinfo, after: datetime, expected: datetime) -> None:
    found = tz_helpers.next_utc_offset_change(zone, after)

    assert found is not None
    # The scan resolves to the minute, which is ample for a rule expressed in
    # whole hours.
    assert abs(found - expected) <= timedelta(minutes=1), (
        f"expected {expected.isoformat()}, got {found.isoformat()}"
    )


@pytest.mark.parametrize("zone", [UTC, KOLKATA])
def test_zones_without_daylight_saving(zone: tzinfo) -> None:
    assert tz_helpers.next_utc_offset_change(zone, utc(2026, 1, 1)) is None


def test_transition_is_strictly_after_the_starting_point() -> None:
    """Called again right after a transition, the next one must be returned."""
    spring = tz_helpers.next_utc_offset_change(PARIS, utc(2026, 1, 1))

    following = tz_helpers.next_utc_offset_change(PARIS, spring)

    assert following is not None
    assert following > spring
    assert following.month == 10, "the autumn transition should come next"


def test_horizon_limits_the_search() -> None:
    assert (
        tz_helpers.next_utc_offset_change(
            PARIS, utc(2026, 4, 1), horizon=timedelta(days=30)
        )
        is None
    ), "the next Paris transition is six months away"


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError):
        tz_helpers.next_utc_offset_change(PARIS, datetime(2026, 1, 1))


def test_accepts_a_non_utc_starting_point() -> None:
    """The caller may pass local time; the result is still UTC."""
    found = tz_helpers.next_utc_offset_change(
        PARIS, datetime(2026, 1, 1, tzinfo=PARIS)
    )

    assert found is not None
    assert found.utcoffset() == timedelta(0)
    assert abs(found - utc(2026, 3, 29, 1, 0)) <= timedelta(minutes=1)


# ------------------------------------------------- a synthetic, odd rule


class _HalfHourDST(tzinfo):
    """A zone shifting by 30 minutes, to prove nothing assumes a whole hour."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=10) + self.dst(dt)

    def dst(self, dt: datetime | None) -> timedelta:
        if dt is None:
            return timedelta()
        return timedelta(minutes=30) if dt.month == 6 else timedelta()

    def tzname(self, dt: datetime | None) -> str:
        return "HALF"


def test_handles_a_non_hourly_shift() -> None:
    zone = _HalfHourDST()

    found = tz_helpers.next_utc_offset_change(zone, utc(2026, 5, 1))

    assert found is not None
    assert tz_helpers.utc_offset(zone, found) == timedelta(hours=10, minutes=30)
    assert tz_helpers.is_dst(zone, found) is True

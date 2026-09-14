"""Keeping the controller clock right across daylight-saving transitions."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_accesscontrol.api import UhppoteTimeout
from custom_components.ha_accesscontrol.const import CONF_SYNC_CLOCK, DOMAIN
from custom_components.ha_accesscontrol.coordinator import (
    CLOCK_SYNC_MARGIN,
    CLOCK_SYNC_RETRY,
)

from .conftest import make_status


def coordinator_of(hass: HomeAssistant, entry):
    return hass.data[DOMAIN][entry.entry_id]


async def fire_at(hass: HomeAssistant, moment: datetime) -> None:
    """Jump to ``moment`` so timers scheduled for it run."""
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


def drifted_status(seconds: float):
    """A status whose clock is ``seconds`` away from Home Assistant's."""
    return make_status(
        controller_time=(
            dt_util.now() + timedelta(seconds=seconds)
        ).replace(tzinfo=None)
    )


# ------------------------------------------------------------- scheduling


async def test_next_transition_is_armed_by_default(
    hass: HomeAssistant, entry
) -> None:
    """Home Assistant's test timezone is US/Pacific, which observes DST."""
    coordinator = coordinator_of(hass, entry)

    assert coordinator.sync_clock is True
    assert coordinator.next_clock_sync is not None
    assert coordinator.next_clock_sync > dt_util.utcnow()


async def test_nothing_is_armed_when_the_option_is_off(
    hass: HomeAssistant, setup_entry, controller
) -> None:
    """Opting out must also suppress the start-up catch-up.

    The controller is given an hour of drift, so a write would happen if the
    option were ignored.
    """
    controller.status = drifted_status(3600)

    entry = await setup_entry(**{CONF_SYNC_CLOCK: False})

    coordinator = coordinator_of(hass, entry)
    assert coordinator.next_clock_sync is None, "no transition timer"
    assert controller.time_writes == [], "and no catch-up either"


async def test_scheduling_refuses_when_the_option_is_off(
    hass: HomeAssistant, entry
) -> None:
    """The coordinator guards itself, not just its caller."""
    coordinator = coordinator_of(hass, entry)
    assert coordinator.next_clock_sync is not None

    coordinator.sync_clock = False
    coordinator.async_schedule_clock_sync()

    assert coordinator.next_clock_sync is None


async def test_clock_is_written_at_the_transition(
    hass: HomeAssistant, entry, controller
) -> None:
    coordinator = coordinator_of(hass, entry)
    transition = coordinator.next_clock_sync
    assert controller.time_writes == [], "nothing to do before the transition"

    await fire_at(hass, transition + CLOCK_SYNC_MARGIN + timedelta(seconds=1))

    assert len(controller.time_writes) == 1, "the clock was not rewritten"
    assert controller.time_writes[0].tzinfo is None


async def test_the_following_transition_is_armed(
    hass: HomeAssistant, entry, controller
) -> None:
    """Otherwise the clock would only ever be corrected once."""
    coordinator = coordinator_of(hass, entry)
    first = coordinator.next_clock_sync

    await fire_at(hass, first + CLOCK_SYNC_MARGIN + timedelta(seconds=1))

    assert coordinator.next_clock_sync is not None
    assert coordinator.next_clock_sync > first


async def test_written_time_follows_the_new_offset(
    hass: HomeAssistant, entry, controller, freezer
) -> None:
    """The point of the exercise: the wall clock sent must be the new one.

    Time is frozen rather than merely fired, so that dt_util.now() inside the
    coordinator really is the instant after the transition.
    """
    coordinator = coordinator_of(hass, entry)
    transition = coordinator.next_clock_sync
    zone = dt_util.DEFAULT_TIME_ZONE

    before = (transition - timedelta(minutes=1)).astimezone(zone)
    after = (transition + timedelta(minutes=1)).astimezone(zone)
    assert before.utcoffset() != after.utcoffset(), "not an offset change"

    freezer.move_to(transition + CLOCK_SYNC_MARGIN + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    written = controller.time_writes[0]
    expected = dt_util.now().replace(tzinfo=None)
    assert abs(written - expected) < timedelta(seconds=5), (
        f"wrote {written}, expected local wall clock {expected}"
    )
    # And that local wall clock is the one on the far side of the transition.
    assert abs(written - after.replace(tzinfo=None)) < timedelta(minutes=5), (
        f"wrote {written}, which is not the post-transition wall clock {after}"
    )


async def test_a_failed_transition_retries_soon(
    hass: HomeAssistant, entry, controller
) -> None:
    """A controller that was unreachable must not wait for the next transition."""
    coordinator = coordinator_of(hass, entry)
    transition = coordinator.next_clock_sync

    async def _fail(moment):
        raise UhppoteTimeout("no answer")

    controller.set_time = _fail
    await fire_at(hass, transition + CLOCK_SYNC_MARGIN + timedelta(seconds=1))

    assert controller.time_writes == []

    # The retry is minutes away, not six months.
    controller.set_time = type(controller).set_time.__get__(controller)
    await fire_at(
        hass,
        dt_util.utcnow() + CLOCK_SYNC_RETRY + timedelta(seconds=1),
    )

    assert len(controller.time_writes) == 1


async def test_timezone_change_rearms_the_timer(
    hass: HomeAssistant, entry
) -> None:
    coordinator = coordinator_of(hass, entry)
    before = coordinator.next_clock_sync

    await hass.config.async_update(time_zone="Europe/Paris")
    hass.bus.async_fire(EVENT_CORE_CONFIG_UPDATE)
    await hass.async_block_till_done()

    assert coordinator.next_clock_sync is not None
    assert coordinator.next_clock_sync != before, (
        "Europe/Paris and US/Pacific do not switch on the same day"
    )


async def test_shutdown_cancels_the_timer(
    hass: HomeAssistant, entry, controller
) -> None:
    coordinator = coordinator_of(hass, entry)
    transition = coordinator.next_clock_sync

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await fire_at(hass, transition + CLOCK_SYNC_MARGIN + timedelta(seconds=1))

    assert controller.time_writes == [], "an unloaded entry must not write"


# ---------------------------------------------------------------- catch-up


async def test_a_missed_transition_is_caught_at_startup(
    hass: HomeAssistant, setup_entry, controller
) -> None:
    """Home Assistant may have been down when the change happened."""
    controller.status = drifted_status(3600)

    await setup_entry()

    assert len(controller.time_writes) == 1, "an hour of drift must be corrected"


@pytest.mark.parametrize("drift", [0, 30, -30, 59])
async def test_small_drift_is_left_alone(
    hass: HomeAssistant, setup_entry, controller, drift: float
) -> None:
    controller.status = drifted_status(drift)

    await setup_entry()

    assert controller.time_writes == []


@pytest.mark.parametrize("drift", [120, -120, -3600])
async def test_large_drift_is_corrected(
    hass: HomeAssistant, setup_entry, controller, drift: float
) -> None:
    controller.status = drifted_status(drift)

    await setup_entry()

    assert len(controller.time_writes) == 1


async def test_no_catch_up_without_a_readable_clock(
    hass: HomeAssistant, setup_entry, controller
) -> None:
    """Older firmware omits the date, so drift cannot be measured."""
    controller.status = make_status(controller_time=None)

    entry = await setup_entry()

    assert controller.time_writes == [], "nothing to compare against"
    assert coordinator_of(hass, entry).next_clock_sync is not None, (
        "the scheduled resync still applies, and does not need the date"
    )


async def test_catch_up_failure_does_not_break_setup(
    hass: HomeAssistant, setup_entry, controller
) -> None:
    controller.status = drifted_status(3600)

    async def _fail(moment):
        raise UhppoteTimeout("no answer")

    controller.set_time = _fail
    entry = await setup_entry()

    assert entry.state is ConfigEntryState.LOADED, (
        "an unreachable clock must not prevent the entry from loading"
    )
    assert coordinator_of(hass, entry).next_clock_sync is not None, (
        "and the transition timer must still be armed"
    )


# ------------------------------------------------------------- reporting


async def test_drift_sensor_reports_daylight_saving(
    hass: HomeAssistant, entry, entity_id_for
) -> None:
    state = hass.states.get(entity_id_for("sensor", "223000123_clock_drift"))

    assert "dst_active" in state.attributes
    assert isinstance(state.attributes["dst_active"], bool)
    assert state.attributes["next_clock_sync"] is not None
    # Parsing it back proves it is a usable timestamp, not a repr.
    assert dt_util.parse_datetime(state.attributes["next_clock_sync"]) is not None


async def test_drift_sensor_value(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    controller.status = drifted_status(-45)

    await setup_entry()

    state = hass.states.get(entity_id_for("sensor", "223000123_clock_drift"))
    assert -50 < float(state.state) < -40

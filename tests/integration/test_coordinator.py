"""Coordinator behaviour: events, push handling, and the open-pulse refresh."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.ha_accesscontrol.api import (
    RECORD_ALARM,
    RECORD_NONE,
    RECORD_OVERWRITTEN,
)
from custom_components.ha_accesscontrol.const import EVENT_RECORD

from .conftest import EVENT_WALL_CLOCK, make_status


async def _advance(hass: HomeAssistant, seconds: float) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


def _capture_events(hass: HomeAssistant) -> list:
    events: list = []
    hass.bus.async_listen(EVENT_RECORD, events.append)
    return events


# ---------------------------------------------------------------- events


async def test_no_event_replayed_on_first_refresh(
    hass: HomeAssistant, setup_entry, controller
) -> None:
    """The controller always reports its last record; it is not news."""
    events = _capture_events(hass)
    controller.status = make_status(last_record_index=4200)

    await setup_entry()

    assert events == [], "the record already stored must not be fired at startup"


async def test_event_fired_when_a_new_record_appears(
    hass: HomeAssistant, entry, controller
) -> None:
    events = _capture_events(hass)
    controller.status = make_status(
        last_record_index=2, card_number=987654, door=2, granted=False, direction=2
    )

    await _advance(hass, 30)

    assert len(events) == 1
    data = events[0].data
    assert data["serial"] == 223000123
    assert data["index"] == 2
    assert data["record_type"] == "card"
    assert data["granted"] is False
    assert data["door"] == 2
    assert data["direction"] == "out"
    assert data["card_number"] == 987654
    # Timezone-independent: the controller's wall clock must survive the
    # round trip, which it would not if it were read as UTC.
    assert dt_util.as_local(
        dt_util.parse_datetime(data["timestamp"])
    ).replace(tzinfo=None) == EVENT_WALL_CLOCK


async def test_no_event_when_the_index_is_unchanged(
    hass: HomeAssistant, entry, controller
) -> None:
    events = _capture_events(hass)

    await _advance(hass, 30)
    await _advance(hass, 30)

    assert events == [], "polling the same record must stay silent"


async def test_no_event_for_empty_or_overwritten_records(
    hass: HomeAssistant, entry, controller
) -> None:
    events = _capture_events(hass)

    controller.status = make_status(last_record_index=2, record_type=RECORD_NONE)
    await _advance(hass, 30)
    controller.status = make_status(last_record_index=3, record_type=RECORD_OVERWRITTEN)
    await _advance(hass, 30)

    assert events == []


async def test_alarm_records_are_reported(
    hass: HomeAssistant, entry, controller
) -> None:
    events = _capture_events(hass)
    controller.status = make_status(last_record_index=9, record_type=RECORD_ALARM)

    await _advance(hass, 30)

    assert [e.data["record_type"] for e in events] == ["alarm"]


# ------------------------------------------------------------------ push


async def test_push_updates_state_without_polling(
    hass: HomeAssistant, setup_entry, controller, listener, entity_id_for
) -> None:
    await setup_entry(push_enabled=True, push_port=60002, push_interval=30)
    events = _capture_events(hass)
    polls_before = controller.status_calls

    handle = listener.callbacks[223000123]
    handle(make_status(last_record_index=77, door_sensors=(True, False, False, False)))
    await hass.async_block_till_done()

    assert controller.status_calls == polls_before, "no extra poll was needed"
    assert len(events) == 1 and events[0].data["index"] == 77
    contact = entity_id_for("binary_sensor", "223000123_door_1_contact")
    assert hass.states.get(contact).state == "on"


# ----------------------------------------------------------- open pulse


async def test_open_schedules_a_refresh_after_the_pulse(
    hass: HomeAssistant, entry, controller, entity_id_for
) -> None:
    """The relay falls back after the open delay; the state must follow.

    Without the scheduled refresh the relay sensor would stay stale until the
    next poll.
    """
    controller.status = make_status(relays=(True, False, False, False))
    relay = entity_id_for("binary_sensor", "223000123_door_1_relay")

    await hass.services.async_call(
        "lock", "open", {ATTR_ENTITY_ID: entity_id_for("lock", "223000123_door_1_lock")},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get(relay).state == "on"

    # The controller releases the relay at the end of the 3 s delay.
    controller.status = make_status(relays=(False, False, False, False))
    calls_before = controller.status_calls

    await _advance(hass, 4.5)

    assert controller.status_calls > calls_before, "the pulse refresh did not fire"
    assert hass.states.get(relay).state == "off"


async def test_pulse_refresh_uses_the_configured_delay(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    from custom_components.ha_accesscontrol.api import DoorConfig

    controller.door_configs[1] = DoorConfig(door=1, mode=3, delay=20)
    await setup_entry()

    await hass.services.async_call(
        "lock", "open", {ATTR_ENTITY_ID: entity_id_for("lock", "223000123_door_1_lock")},
        blocking=True,
    )
    await hass.async_block_till_done()
    calls_before = controller.status_calls

    await _advance(hass, 5)
    assert controller.status_calls == calls_before, "too early for a 20 s pulse"

    await _advance(hass, 17)
    assert controller.status_calls > calls_before


# ------------------------------------------------------------ availability


async def test_entities_go_unavailable_when_the_controller_stops_answering(
    hass: HomeAssistant, entry, controller, entity_id_for, timeout_error
) -> None:
    contact = entity_id_for("binary_sensor", "223000123_door_1_contact")
    assert hass.states.get(contact).state == "off"

    controller.status_error = timeout_error
    await _advance(hass, 30)

    assert hass.states.get(contact).state == "unavailable"


async def test_coordinator_recovers_after_a_failure(
    hass: HomeAssistant, entry, controller, entity_id_for, timeout_error
) -> None:
    contact = entity_id_for("binary_sensor", "223000123_door_1_contact")

    controller.status_error = timeout_error
    await _advance(hass, 30)
    assert hass.states.get(contact).state == "unavailable"

    controller.status_error = None
    controller.status = make_status(door_sensors=(True, False, False, False))
    await _advance(hass, 30)

    assert hass.states.get(contact).state == "on"

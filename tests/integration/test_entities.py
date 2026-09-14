"""Sensors, binary sensors and the open-delay number."""

from __future__ import annotations

from datetime import datetime

import pytest
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_accesscontrol.api import (
    DOOR_MODE_NORMALLY_CLOSED,
    DOOR_MODE_NORMALLY_OPEN,
    DoorConfig,
    UhppoteTimeout,
)

from .conftest import EVENT_WALL_CLOCK, make_status

# ----------------------------------------------------------- binary sensors


@pytest.mark.parametrize(
    ("unique_id", "status_kwargs", "expected"),
    [
        ("223000123_door_1_contact", {"door_sensors": (True, False, False, False)}, "on"),
        ("223000123_door_2_contact", {"door_sensors": (True, False, False, False)}, "off"),
        ("223000123_door_1_button", {"buttons": (True, False, False, False)}, "on"),
        ("223000123_door_1_relay", {"relays": (True, False, False, False)}, "on"),
        ("223000123_fire", {"fire": True}, "on"),
        ("223000123_forced_lock", {"forced_lock": True}, "on"),
        ("223000123_error", {"error_code": 7}, "on"),
        ("223000123_error", {"error_code": 0}, "off"),
    ],
)
async def test_binary_sensor_states(
    hass: HomeAssistant,
    setup_entry,
    controller,
    entity_id_for,
    unique_id: str,
    status_kwargs: dict,
    expected: str,
) -> None:
    controller.status = make_status(**status_kwargs)

    await setup_entry()

    assert hass.states.get(entity_id_for("binary_sensor", unique_id)).state == expected


async def test_door_sensors_are_limited_to_the_real_doors(
    hass: HomeAssistant, entry
) -> None:
    registry = er.async_get(hass)
    contacts = [
        item.unique_id
        for item in registry.entities.values()
        if item.unique_id.endswith("_contact")
    ]

    assert sorted(contacts) == [
        "223000123_door_1_contact",
        "223000123_door_2_contact",
    ]


# ------------------------------------------------------------------ sensors


async def test_sensor_states(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    controller.status = make_status(
        card_number=987654, door=2, direction=2, last_record_index=1234
    )

    await setup_entry()

    def value(unique_id: str) -> str:
        return hass.states.get(entity_id_for("sensor", unique_id)).state

    assert value("223000123_last_card") == "987654"
    assert value("223000123_last_door") == "2"
    assert value("223000123_last_direction") == "out"
    assert value("223000123_last_record_type") == "card"
    assert value("223000123_last_record_index") == "1234"
    # The state is rendered in UTC; convert back and compare wall clocks.
    from homeassistant.util import dt as dt_util

    assert dt_util.as_local(
        dt_util.parse_datetime(value("223000123_last_event_time"))
    ).replace(tzinfo=None) == EVENT_WALL_CLOCK


async def test_last_card_is_blank_for_non_card_records(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    from custom_components.ha_accesscontrol.api import RECORD_DOOR

    controller.status = make_status(record_type=RECORD_DOOR, card_number=42)

    await setup_entry()

    state = hass.states.get(entity_id_for("sensor", "223000123_last_card"))
    assert state.state == "unknown", "a door record carries a code, not a card"


async def test_clock_drift(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    from homeassistant.util import dt as dt_util

    now = dt_util.now()
    controller.status = make_status(
        controller_time=now.replace(tzinfo=None) - __import__(
            "datetime"
        ).timedelta(seconds=45)
    )

    await setup_entry()

    drift = float(hass.states.get(entity_id_for("sensor", "223000123_clock_drift")).state)
    assert -50 < drift < -40, f"expected roughly -45 s, got {drift}"


async def test_clock_drift_unknown_on_old_firmware(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    controller.status = make_status(controller_time=None)

    await setup_entry()

    state = hass.states.get(entity_id_for("sensor", "223000123_clock_drift"))
    assert state.state == "unknown"


# ------------------------------------------------------------------- number


async def test_open_delay_number(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_CLOSED, delay=12
    )

    await setup_entry()

    entity_id = entity_id_for("number", "223000123_door_1_open_delay")
    state = hass.states.get(entity_id)
    assert state.state == "12.0"
    assert state.attributes["unit_of_measurement"] == "s"
    assert state.attributes["min"] == 0
    assert state.attributes["max"] == 255

    registry_entry = er.async_get(hass).async_get(entity_id)
    assert registry_entry.entity_category is EntityCategory.CONFIG


async def test_setting_the_delay_preserves_the_control_mode(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """Function 0x80 carries both fields, so the mode must be resent intact."""
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_CLOSED, delay=3
    )
    await setup_entry()
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=3
    )

    await hass.services.async_call(
        "number",
        "set_value",
        {
            ATTR_ENTITY_ID: entity_id_for("number", "223000123_door_1_open_delay"),
            "value": 25,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert controller.door_configs[1] == DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=25
    )


async def test_delay_write_failure_surfaces_as_an_error(
    hass: HomeAssistant, entry, controller, entity_id_for
) -> None:
    async def _fail(door: int, mode: int, delay: int):
        raise UhppoteTimeout("no answer")

    controller.set_door_config = _fail

    with pytest.raises(HomeAssistantError, match="no answer"):
        await hass.services.async_call(
            "number",
            "set_value",
            {
                ATTR_ENTITY_ID: entity_id_for("number", "223000123_door_1_open_delay"),
                "value": 7,
            },
            blocking=True,
        )


# ------------------------------------------------------------------ button


async def test_no_per_door_button_exists(hass: HomeAssistant, entry) -> None:
    """Opening a door is lock.open; a second entity for it would be noise."""
    registry = er.async_get(hass)
    buttons = [
        item.unique_id
        for item in registry.entities.values()
        if item.domain == "button"
    ]

    assert buttons == ["223000123_sync_time"]


async def test_sync_time_button(
    hass: HomeAssistant, entry, controller, entity_id_for
) -> None:
    entity_id = entity_id_for("button", "223000123_sync_time")
    registry_entry = er.async_get(hass).async_get(entity_id)

    assert registry_entry.entity_category is EntityCategory.CONFIG

    await hass.services.async_call(
        "button",
        "press",
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert len(controller.time_writes) == 1
    assert controller.time_writes[0].tzinfo is None


# ---------------------------------------------------------------- services


async def test_sync_time_service(hass: HomeAssistant, entry, controller) -> None:
    await hass.services.async_call(
        "ha_accesscontrol", "sync_time", {}, blocking=True
    )
    await hass.async_block_till_done()

    assert len(controller.time_writes) == 1
    written = controller.time_writes[0]
    assert isinstance(written, datetime)
    assert written.tzinfo is None, "the protocol carries a naive local time"


async def test_set_door_mode_service(hass: HomeAssistant, entry, controller) -> None:
    await hass.services.async_call(
        "ha_accesscontrol",
        "set_door_mode",
        {"door": 1, "mode": "normally_open", "delay": 9},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert controller.door_configs[1] == DoorConfig(door=1, mode=1, delay=9)

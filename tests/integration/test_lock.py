"""The lock entity and the control-mode semantics it exposes."""

from __future__ import annotations

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_LOCKED,
    STATE_UNAVAILABLE,
    STATE_UNLOCKED,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_accesscontrol.api import (
    DOOR_MODE_CONTROLLED,
    DOOR_MODE_NORMALLY_CLOSED,
    DOOR_MODE_NORMALLY_OPEN,
    DoorConfig,
    UhppoteRefused,
)

from .conftest import make_status


@pytest.fixture
def lock_1(entity_id_for) -> str:
    return entity_id_for("lock", "223000123_door_1_lock")


async def _call(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        "lock", service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    await hass.async_block_till_done()


# ------------------------------------------------------------------- state


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (DOOR_MODE_NORMALLY_OPEN, STATE_UNLOCKED),
        (DOOR_MODE_NORMALLY_CLOSED, STATE_LOCKED),
        (DOOR_MODE_CONTROLLED, STATE_LOCKED),
    ],
)
async def test_state_follows_the_control_mode(
    hass: HomeAssistant, setup_entry, controller, entity_id_for, mode, expected
) -> None:
    controller.door_configs[1] = DoorConfig(door=1, mode=mode, delay=3)

    await setup_entry()

    entity_id = entity_id_for("lock", "223000123_door_1_lock")
    assert hass.states.get(entity_id).state == expected


async def test_state_ignores_the_momentary_relay(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """A relay pulse must not flip the lock: that is the whole design choice.

    The relay is released for the open delay on every valid badge read; a lock
    entity tracking it would be useless in automations.
    """
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=3
    )
    controller.status = make_status(relays=(True, False, False, False))

    await setup_entry()

    entity_id = entity_id_for("lock", "223000123_door_1_lock")
    state = hass.states.get(entity_id)
    assert state.state == STATE_LOCKED
    assert state.attributes["relay"] is True, (
        "the relay is still reported, just not as the lock state"
    )


async def test_attributes_expose_mode_and_delay(
    hass: HomeAssistant, entry, lock_1
) -> None:
    attributes = hass.states.get(lock_1).attributes

    assert attributes["door"] == 1
    assert attributes["control_mode"] == "controlled"
    assert attributes["open_delay"] == 3


async def test_unavailable_when_the_door_config_is_unknown(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    # The controller never answers for this door; loading must not fail the
    # whole entry, the entity just has nothing to report.
    controller.door_configs.pop(1)

    await setup_entry()

    entity_id = entity_id_for("lock", "223000123_door_1_lock")
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(entity_id_for("lock", "223000123_door_2_lock")).state == (
        STATE_LOCKED
    ), "the other door is unaffected"


# ---------------------------------------------------------------- commands


async def test_lock_switches_to_normally_closed(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    await _call(hass, "lock", lock_1)

    assert controller.door_configs[1].mode == DOOR_MODE_NORMALLY_CLOSED
    assert hass.states.get(lock_1).state == STATE_LOCKED


async def test_unlock_switches_to_normally_open(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    await _call(hass, "unlock", lock_1)

    assert controller.door_configs[1].mode == DOOR_MODE_NORMALLY_OPEN
    assert hass.states.get(lock_1).state == STATE_UNLOCKED


async def test_mode_changes_preserve_the_open_delay(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """Function 0x80 carries both fields, so the delay must be resent intact."""
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=17
    )

    await setup_entry()
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=23
    )
    await _call(hass, "unlock", entity_id_for("lock", "223000123_door_1_lock"))

    assert controller.door_configs[1] == DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=23
    )


async def test_open_pulses_without_changing_the_mode(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    await _call(hass, "open", lock_1)

    assert controller.opened == [1]
    assert controller.door_configs[1].mode == DOOR_MODE_CONTROLLED
    assert hass.states.get(lock_1).state == STATE_LOCKED


async def test_open_failure_surfaces_as_an_error(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    controller.open_error = UhppoteRefused("door wired as normally closed")

    with pytest.raises(HomeAssistantError, match="normally closed"):
        await _call(hass, "open", lock_1)


async def test_only_configured_doors_get_a_lock(
    hass: HomeAssistant, entry, entity_id_for
) -> None:
    """The serial number says two doors, so doors 3 and 4 must not appear."""
    registry = er.async_get(hass)
    locks = [
        entity.unique_id
        for entity in registry.entities.values()
        if entity.domain == "lock"
    ]

    assert sorted(locks) == [
        "223000123_door_1_lock",
        "223000123_door_2_lock",
    ]

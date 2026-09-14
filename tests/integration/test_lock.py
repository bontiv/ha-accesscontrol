"""The lock entity and the control-mode semantics it exposes."""

from __future__ import annotations

import pytest
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_LOCKED,
    STATE_OPEN,
    STATE_UNAVAILABLE,
    STATE_UNLOCKED,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.ha_accesscontrol.const import CONF_OPEN_SOURCE, DOMAIN

from custom_components.ha_accesscontrol.api import (
    DOOR_MODE_CONTROLLED,
    DOOR_MODE_NORMALLY_CLOSED,
    DOOR_MODE_NORMALLY_OPEN,
    DoorConfig,
    UhppoteRefused,
)

from .conftest import make_status


def coordinator_of(hass: HomeAssistant, entry):
    return hass.data[DOMAIN][entry.entry_id]


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


async def test_released_relay_reads_as_open(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """A badge read releases the relay, and the door really is open then.

    ``open`` rather than ``unlocked``: the control mode has not changed, so
    the door will close again by itself at the end of the pulse.
    """
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=3
    )
    controller.status = make_status(relays=(True, False, False, False))

    await setup_entry()

    state = hass.states.get(entity_id_for("lock", "223000123_door_1_lock"))
    assert state.state == STATE_OPEN
    assert state.attributes["control_mode"] == "controlled", (
        "the pulse must not look like a reconfiguration"
    )
    assert state.attributes["relay"] is True


async def test_other_doors_are_unaffected_by_a_pulse(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    controller.status = make_status(relays=(True, False, False, False))

    await setup_entry()

    assert hass.states.get(entity_id_for("lock", "223000123_door_2_lock")).state == (
        STATE_LOCKED
    )


async def test_normally_open_reads_as_unlocked_not_open(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """Held open by configuration is not the same as a momentary release.

    In normally-open mode the relay is energised permanently, so reporting it
    as ``open`` would make ``unlocked`` unreachable.
    """
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=3
    )
    controller.status = make_status(relays=(True, False, False, False))

    await setup_entry()

    state = hass.states.get(entity_id_for("lock", "223000123_door_1_lock"))
    assert state.state == STATE_UNLOCKED
    assert state.attributes["relay"] is True


@pytest.mark.parametrize(
    ("mode", "relay", "expected"),
    [
        (DOOR_MODE_CONTROLLED, False, STATE_LOCKED),
        (DOOR_MODE_CONTROLLED, True, STATE_OPEN),
        (DOOR_MODE_NORMALLY_CLOSED, False, STATE_LOCKED),
        (DOOR_MODE_NORMALLY_CLOSED, True, STATE_OPEN),
        (DOOR_MODE_NORMALLY_OPEN, False, STATE_UNLOCKED),
        (DOOR_MODE_NORMALLY_OPEN, True, STATE_UNLOCKED),
    ],
)
async def test_state_matrix(
    hass: HomeAssistant,
    setup_entry,
    controller,
    entity_id_for,
    mode: int,
    relay: bool,
    expected: str,
) -> None:
    controller.door_configs[1] = DoorConfig(door=1, mode=mode, delay=3)
    controller.status = make_status(relays=(relay, False, False, False))

    await setup_entry()

    assert hass.states.get(
        entity_id_for("lock", "223000123_door_1_lock")
    ).state == expected


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


# ------------------------------------------------- source of "open"


async def test_relay_is_the_default_source(hass: HomeAssistant, entry) -> None:
    assert coordinator_of(hass, entry).open_source == "relay"


@pytest.mark.parametrize(
    ("mode", "relay", "contact", "expected"),
    [
        # The leaf is shut but the relay is released: commanded open.
        (DOOR_MODE_CONTROLLED, True, False, STATE_OPEN),
        # Someone is holding the door after the pulse ended: not the relay's
        # business, so it reads locked again.
        (DOOR_MODE_CONTROLLED, False, True, STATE_LOCKED),
        (DOOR_MODE_CONTROLLED, False, False, STATE_LOCKED),
        (DOOR_MODE_NORMALLY_OPEN, True, True, STATE_UNLOCKED),
    ],
)
async def test_relay_source(
    hass: HomeAssistant,
    setup_entry,
    controller,
    entity_id_for,
    mode: int,
    relay: bool,
    contact: bool,
    expected: str,
) -> None:
    controller.door_configs[1] = DoorConfig(door=1, mode=mode, delay=3)
    controller.status = make_status(
        relays=(relay, False, False, False),
        door_sensors=(contact, False, False, False),
    )

    await setup_entry()

    assert hass.states.get(
        entity_id_for("lock", "223000123_door_1_lock")
    ).state == expected


@pytest.mark.parametrize(
    ("mode", "relay", "contact", "expected"),
    [
        # Commanded open but the leaf never moved: nobody went through.
        (DOOR_MODE_CONTROLLED, True, False, STATE_LOCKED),
        # The leaf is open, whatever the relay is doing.
        (DOOR_MODE_CONTROLLED, False, True, STATE_OPEN),
        (DOOR_MODE_CONTROLLED, False, False, STATE_LOCKED),
        # Released by configuration, but shut: unlocked, not open.
        (DOOR_MODE_NORMALLY_OPEN, True, False, STATE_UNLOCKED),
        # Released by configuration and actually open.
        (DOOR_MODE_NORMALLY_OPEN, True, True, STATE_OPEN),
    ],
)
async def test_door_contact_source(
    hass: HomeAssistant,
    setup_entry,
    controller,
    entity_id_for,
    mode: int,
    relay: bool,
    contact: bool,
    expected: str,
) -> None:
    controller.door_configs[1] = DoorConfig(door=1, mode=mode, delay=3)
    controller.status = make_status(
        relays=(relay, False, False, False),
        door_sensors=(contact, False, False, False),
    )

    await setup_entry(**{CONF_OPEN_SOURCE: "door_contact"})

    assert hass.states.get(
        entity_id_for("lock", "223000123_door_1_lock")
    ).state == expected


async def test_each_door_reads_its_own_contact(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """Door 2 being open must not make door 1 look open."""
    controller.status = make_status(
        relays=(False, False, False, False),
        door_sensors=(False, True, False, False),
    )

    await setup_entry(**{CONF_OPEN_SOURCE: "door_contact"})

    assert hass.states.get(
        entity_id_for("lock", "223000123_door_1_lock")
    ).state == STATE_LOCKED
    assert hass.states.get(
        entity_id_for("lock", "223000123_door_2_lock")
    ).state == STATE_OPEN


async def test_source_is_reported_in_the_attributes(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """Both signals stay visible, whichever one drives the state."""
    controller.status = make_status(
        relays=(True, False, False, False),
        door_sensors=(False, False, False, False),
    )

    await setup_entry(**{CONF_OPEN_SOURCE: "door_contact"})

    attributes = hass.states.get(
        entity_id_for("lock", "223000123_door_1_lock")
    ).attributes
    assert attributes["open_source"] == "door_contact"
    assert attributes["relay"] is True
    assert attributes["door_contact"] is False


# ---------------------------------------------------------------- commands


async def test_lock_returns_to_online_control(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    """Locking means "no longer held open", never a lockdown.

    Normally closed ignores cards and refuses remote opening, so reaching it
    by tapping the lock tile would take the door out of service silently:
    the entity reads ``locked`` in that mode exactly as it does in online
    control.
    """
    await _call(hass, "lock", lock_1)

    assert controller.door_configs[1].mode == DOOR_MODE_CONTROLLED
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
    await _call(hass, "unlock", entity_id_for("lock", "223000123_door_1_lock"))

    assert controller.door_configs[1] == DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=17
    )


async def test_unlock_then_lock_restores_the_open_delay(
    hass: HomeAssistant, setup_entry, controller, entity_id_for
) -> None:
    """The round trip must not cost the door its pulse length.

    A controller held normally open reports no usable open delay, so the
    delay read back at that point is worthless. Sending it along with the
    next mode would write it into the configuration for good, leaving a
    relay that barely clicks.
    """
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=17
    )
    await setup_entry()
    lock_1 = entity_id_for("lock", "223000123_door_1_lock")

    await _call(hass, "unlock", lock_1)
    # What the firmware answers once the door is held open.
    controller.door_configs[1] = DoorConfig(
        door=1, mode=DOOR_MODE_NORMALLY_OPEN, delay=0
    )

    await _call(hass, "lock", lock_1)

    assert controller.door_configs[1] == DoorConfig(
        door=1, mode=DOOR_MODE_CONTROLLED, delay=17
    )


async def test_open_pulses_without_changing_the_mode(
    hass: HomeAssistant, entry, controller, lock_1
) -> None:
    await _call(hass, "open", lock_1)

    assert controller.opened == [1]
    assert controller.door_configs[1].mode == DOOR_MODE_CONTROLLED, (
        "a pulse must leave the persistent configuration alone"
    )
    assert hass.states.get(lock_1).state == STATE_OPEN, (
        "the relay is released, so the door reads as open, not unlocked"
    )


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

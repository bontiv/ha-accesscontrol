"""Binary sensors derived from the controller status packet (function 0x20)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ControllerStatus
from .const import DOMAIN
from .coordinator import UhppoteCoordinator
from .entity import UhppoteEntity


@dataclass(frozen=True, kw_only=True)
class UhppoteBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a binary sensor backed by the status packet."""

    value_fn: Callable[[ControllerStatus], bool]
    door: int | None = None


CONTROLLER_SENSORS: tuple[UhppoteBinarySensorDescription, ...] = (
    UhppoteBinarySensorDescription(
        key="fire",
        translation_key="fire",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda status: status.fire,
    ),
    UhppoteBinarySensorDescription(
        key="forced_lock",
        translation_key="forced_lock",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=lambda status: status.forced_lock,
    ),
    UhppoteBinarySensorDescription(
        key="error",
        translation_key="error",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda status: status.error_code != 0,
    ),
)


def _door_sensors(door: int) -> tuple[UhppoteBinarySensorDescription, ...]:
    index = door - 1
    return (
        UhppoteBinarySensorDescription(
            key=f"door_{door}_contact",
            translation_key="door_contact",
            door=door,
            device_class=BinarySensorDeviceClass.DOOR,
            value_fn=lambda status: status.door_sensors[index],
        ),
        UhppoteBinarySensorDescription(
            key=f"door_{door}_button",
            translation_key="door_button",
            door=door,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda status: status.buttons[index],
        ),
        UhppoteBinarySensorDescription(
            key=f"door_{door}_relay",
            translation_key="door_relay",
            door=door,
            device_class=BinarySensorDeviceClass.LOCK,
            # Off by default: the lock entity already reports the relay, both
            # as its "open" state and as an attribute, so a second entity for
            # the same signal would only clutter the dashboard. It remains one
            # click away for anyone who wants it on its own -- typically after
            # switching the lock's open state over to the door contact.
            entity_registry_enabled_default=False,
            value_fn=lambda status: status.relays[index],
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary sensors of a controller."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]

    descriptions: list[UhppoteBinarySensorDescription] = list(CONTROLLER_SENSORS)
    for door in range(1, coordinator.doors + 1):
        descriptions.extend(_door_sensors(door))

    async_add_entities(
        UhppoteBinarySensor(coordinator, description) for description in descriptions
    )


class UhppoteBinarySensor(UhppoteEntity, BinarySensorEntity):
    """A boolean field of the controller status packet."""

    entity_description: UhppoteBinarySensorDescription

    def __init__(
        self,
        coordinator: UhppoteCoordinator,
        description: UhppoteBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.door is not None:
            self._attr_translation_placeholders = {"door": str(description.door)}

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

"""Sensors derived from the controller status packet (function 0x20)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RECORD_CARD, ControllerStatus
from .const import DOMAIN, RECORD_TYPES
from .coordinator import UhppoteCoordinator, as_local
from .entity import UhppoteEntity

StateType = str | int | float | datetime | None


@dataclass(frozen=True, kw_only=True)
class UhppoteSensorDescription(SensorEntityDescription):
    """Describe a sensor backed by the status packet."""

    value_fn: Callable[[ControllerStatus], StateType] | None = None
    # Sensors that need more than the status packet read the coordinator.
    coordinator_fn: Callable[["UhppoteCoordinator"], StateType] | None = None
    attributes_fn: Callable[["UhppoteCoordinator"], dict[str, Any]] | None = None


SENSORS: tuple[UhppoteSensorDescription, ...] = (
    UhppoteSensorDescription(
        key="last_card",
        translation_key="last_card",
        icon="mdi:card-account-details",
        value_fn=lambda status: (
            status.card_number if status.record_type == RECORD_CARD else None
        ),
    ),
    UhppoteSensorDescription(
        key="last_door",
        translation_key="last_door",
        icon="mdi:door",
        value_fn=lambda status: status.door or None,
    ),
    UhppoteSensorDescription(
        key="last_direction",
        translation_key="last_direction",
        device_class=SensorDeviceClass.ENUM,
        options=["in", "out"],
        value_fn=lambda status: {1: "in", 2: "out"}.get(status.direction),
    ),
    UhppoteSensorDescription(
        key="last_record_type",
        translation_key="last_record_type",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(RECORD_TYPES.values())),
        value_fn=lambda status: RECORD_TYPES.get(status.record_type),
    ),
    UhppoteSensorDescription(
        key="last_event_time",
        translation_key="last_event_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda status: as_local(status.event_time),
    ),
    UhppoteSensorDescription(
        key="last_record_index",
        translation_key="last_record_index",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:counter",
        value_fn=lambda status: status.last_record_index,
    ),
    UhppoteSensorDescription(
        key="clock_drift",
        translation_key="clock_drift",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-alert-outline",
        coordinator_fn=lambda coordinator: (
            None if (drift := coordinator.clock_drift()) is None else round(drift, 1)
        ),
        attributes_fn=lambda coordinator: {
            "dst_active": coordinator.dst_active,
            "next_clock_sync": (
                coordinator.next_clock_sync.isoformat()
                if coordinator.next_clock_sync
                else None
            ),
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensors of a controller."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        UhppoteSensor(coordinator, description) for description in SENSORS
    )


class UhppoteSensor(UhppoteEntity, SensorEntity):
    """A scalar field of the controller status packet."""

    entity_description: UhppoteSensorDescription

    def __init__(
        self,
        coordinator: UhppoteCoordinator,
        description: UhppoteSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        if self.entity_description.coordinator_fn is not None:
            return self.entity_description.coordinator_fn(self.coordinator)
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator)

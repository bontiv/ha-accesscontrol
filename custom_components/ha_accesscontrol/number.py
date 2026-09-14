"""Configuration numbers for the door control parameters (function 0x80)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UhppoteError
from .const import DOMAIN
from .coordinator import UhppoteCoordinator
from .entity import UhppoteEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one open-delay number per door."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        UhppoteOpenDelayNumber(coordinator, door)
        for door in range(1, coordinator.doors + 1)
    )


class UhppoteOpenDelayNumber(UhppoteEntity, NumberEntity):
    """How long the relay stays released after a valid card or a remote open.

    The value is read back from the controller with function 0x82 and written
    with 0x80. Because that packet carries the control mode alongside the
    delay, the coordinator reads and re-sends the current mode on every write.
    """

    _attr_translation_key = "open_delay"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 255
    _attr_native_step = 1
    _attr_icon = "mdi:timer-lock-open-outline"

    def __init__(self, coordinator: UhppoteCoordinator, door: int) -> None:
        super().__init__(coordinator, f"door_{door}_open_delay")
        self._door = door
        self._attr_translation_placeholders = {"door": str(door)}

    @property
    def available(self) -> bool:
        return super().available and self._door in self.coordinator.door_configs

    @property
    def native_value(self) -> float | None:
        config = self.coordinator.door_configs.get(self._door)
        return None if config is None else float(config.delay)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"door": self._door}

    async def async_set_native_value(self, value: float) -> None:
        """Write the new delay, keeping the door's current control mode."""
        try:
            await self.coordinator.async_set_door_delay(self._door, int(value))
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()

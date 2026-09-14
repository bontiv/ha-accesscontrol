"""Buttons for UHPPOTE controllers."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UhppoteError
from .const import DOMAIN
from .coordinator import UhppoteCoordinator
from .entity import UhppoteEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create controller and door buttons."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            *(
                UhppoteOpenDoorButton(coordinator, door)
                for door in range(1, coordinator.doors + 1)
            ),
            UhppoteSyncTimeButton(coordinator),
        ]
    )


class UhppoteSyncTimeButton(UhppoteEntity, ButtonEntity):
    """Write Home Assistant's current local time to the controller (function 0x30)."""

    _attr_translation_key = "sync_time"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-sync"

    def __init__(self, coordinator: UhppoteCoordinator) -> None:
        super().__init__(coordinator, "sync_time")

    async def async_press(self) -> None:
        """Send the current local time to the controller."""
        try:
            await self.coordinator.async_sync_clock()
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err


class UhppoteOpenDoorButton(UhppoteEntity, ButtonEntity):
    """Trigger remote door opening (function 0x40)."""

    _attr_translation_key = "open_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, coordinator: UhppoteCoordinator, door: int) -> None:
        super().__init__(coordinator, f"door_{door}_open")
        self._door = door
        self._attr_translation_placeholders = {"door": str(door)}

    async def async_press(self) -> None:
        """Send the open-door command."""
        try:
            await self.coordinator.controller.open_door(self._door)
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
        # Catch the relay falling back at the end of the pulse.
        self.coordinator.async_schedule_pulse_refresh(self._door)

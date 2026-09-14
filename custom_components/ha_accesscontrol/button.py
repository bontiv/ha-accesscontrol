"""Open-door buttons for UHPPOTE controllers."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
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
    """Create one open-door button per door."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        UhppoteOpenDoorButton(coordinator, door)
        for door in range(1, coordinator.doors + 1)
    )


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

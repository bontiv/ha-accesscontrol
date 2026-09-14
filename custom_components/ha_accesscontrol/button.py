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
    """Create the controller buttons."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UhppoteSyncTimeButton(coordinator)])


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

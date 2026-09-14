"""Lock entities driving the door control mode (functions 0x80 / 0x82)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import (
    DOOR_MODE_NORMALLY_CLOSED,
    DOOR_MODE_NORMALLY_OPEN,
    UhppoteError,
)
from .const import CODE_TO_MODE, DOMAIN
from .coordinator import UhppoteCoordinator
from .entity import UhppoteEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock entity per door."""
    coordinator: UhppoteCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        UhppoteLock(coordinator, door)
        for door in range(1, coordinator.doors + 1)
    )


class UhppoteLock(UhppoteEntity, LockEntity):
    """Expose a door's control mode as a lock.

    The controller offers two independent notions of "open":

    * the *control mode* (function 0x82) is persistent — normally open,
      normally closed, or controlled by cards and buttons;
    * the *relay* (byte 49 of the status packet) is momentary and falls back
      after the configured open delay.

    ``is_locked`` follows the control mode, because an entity that flipped for
    the three seconds of every badge read would be useless in automations. The
    momentary relay is published separately as a binary sensor, and a one-shot
    pulse is available through ``lock.open``.
    """

    _attr_translation_key = "door"
    _attr_supported_features = LockEntityFeature.OPEN

    def __init__(self, coordinator: UhppoteCoordinator, door: int) -> None:
        super().__init__(coordinator, f"door_{door}_lock")
        self._door = door
        self._attr_translation_placeholders = {"door": str(door)}

    @property
    def _mode(self) -> int | None:
        config = self.coordinator.door_configs.get(self._door)
        return config.mode if config else None

    @property
    def available(self) -> bool:
        return super().available and self._mode is not None

    @property
    def is_locked(self) -> bool | None:
        """A door is considered unlocked only while held normally open."""
        mode = self._mode
        if mode is None:
            return None
        return mode != DOOR_MODE_NORMALLY_OPEN

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = self.coordinator.door_configs.get(self._door)
        attributes: dict[str, Any] = {"door": self._door}
        if config is not None:
            attributes["control_mode"] = CODE_TO_MODE.get(config.mode, config.mode)
            attributes["open_delay"] = config.delay
        if self.coordinator.data is not None:
            attributes["relay"] = self.coordinator.data.relays[self._door - 1]
        return attributes

    async def async_lock(self, **kwargs: Any) -> None:
        """Hold the door normally closed (mode 2)."""
        await self._async_set_mode(DOOR_MODE_NORMALLY_CLOSED)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Hold the door normally open (mode 1).

        This is a persistent change stored in the controller: the door stays
        open across a Home Assistant restart until it is locked again.
        """
        await self._async_set_mode(DOOR_MODE_NORMALLY_OPEN)

    async def async_open(self, **kwargs: Any) -> None:
        """Release the door for the configured delay (function 0x40)."""
        try:
            await self.coordinator.controller.open_door(self._door)
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
        # Catch the relay falling back at the end of the pulse.
        self.coordinator.async_schedule_pulse_refresh(self._door)

    async def _async_set_mode(self, mode: int) -> None:
        try:
            await self.coordinator.async_set_door_mode(self._door, mode)
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()

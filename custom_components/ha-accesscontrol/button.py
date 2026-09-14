"""Open-door buttons for UHPPOTE controllers."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import UhppoteController, UhppoteError
from .const import CONF_DOORS, CONF_SERIAL, DEFAULT_DOORS, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one open-door button per door."""
    controller: UhppoteController = hass.data[DOMAIN][entry.entry_id]
    doors = entry.data.get(CONF_DOORS, DEFAULT_DOORS)

    async_add_entities(
        UhppoteOpenDoorButton(controller, entry, door)
        for door in range(1, doors + 1)
    )


class UhppoteOpenDoorButton(ButtonEntity):
    """Trigger remote door opening (function 0x40)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        controller: UhppoteController,
        entry: ConfigEntry,
        door: int,
    ) -> None:
        self._controller = controller
        self._door = door
        serial = entry.data[CONF_SERIAL]

        self._attr_unique_id = f"{serial}_door_{door}_open"
        self._attr_name = f"Open door {door}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Controller {serial}",
            manufacturer="UHPPOTE",
            model="Wiegand TCP/IP access controller",
            serial_number=str(serial),
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    async def async_press(self) -> None:
        """Send the open-door command."""
        try:
            await self._controller.open_door(self._door)
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err

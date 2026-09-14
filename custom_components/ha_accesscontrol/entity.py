"""Shared entity base for the UHPPOTE integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import UhppoteCoordinator
from .const import DOMAIN


class UhppoteEntity(CoordinatorEntity[UhppoteCoordinator]):
    """Base entity bound to a controller."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: UhppoteCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Controller {serial}",
            manufacturer="UHPPOTE",
            model="Wiegand TCP/IP access controller",
            serial_number=str(serial),
            configuration_url=f"http://{coordinator.controller.host}",
        )

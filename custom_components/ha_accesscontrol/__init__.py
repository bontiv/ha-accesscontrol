"""Home Assistant integration for UHPPOTE access controllers."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import UhppoteController, UhppoteError, discover
from .const import (
    ATTR_DOOR,
    ATTR_SERIAL,
    CONF_BROADCAST_ADDRESS,
    CONF_RETRIES,
    CONF_SERIAL,
    CONF_TIMEOUT,
    DEFAULT_BROADCAST_ADDRESS,
    DEFAULT_PORT,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DOMAIN,
    SERVICE_DISCOVER,
    SERVICE_OPEN_DOOR,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON]

OPEN_DOOR_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SERIAL): vol.Coerce(int),
        vol.Required(ATTR_DOOR): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    }
)

DISCOVER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TIMEOUT, default=3.0): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=15)
        ),
        vol.Optional(
            CONF_BROADCAST_ADDRESS, default=DEFAULT_BROADCAST_ADDRESS
        ): cv.string,
    }
)


def _build_controller(entry: ConfigEntry) -> UhppoteController:
    """Create the client from the config entry data and options."""
    return UhppoteController(
        entry.data[CONF_HOST],
        entry.data[CONF_SERIAL],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        timeout=entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        retries=entry.options.get(CONF_RETRIES, DEFAULT_RETRIES),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a controller."""
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = _build_controller(entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a controller."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _resolve_controller(
    hass: HomeAssistant, serial: int | None
) -> UhppoteController:
    """Resolve the controller targeted by a service call."""
    controllers: list[UhppoteController] = list(hass.data.get(DOMAIN, {}).values())

    if not controllers:
        raise ServiceValidationError("No UHPPOTE controller is configured.")

    if serial is None:
        if len(controllers) > 1:
            raise ServiceValidationError(
                "Multiple controllers are configured; specify the serial parameter."
            )
        return controllers[0]

    for controller in controllers:
        if controller.serial == serial:
            return controller

    known = ", ".join(str(c.serial) for c in controllers)
    raise ServiceValidationError(
        f"No configured controller has serial number {serial} "
        f"(known serial numbers: {known})."
    )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR):
        return

    async def _handle_open_door(call: ServiceCall) -> None:
        controller = _resolve_controller(hass, call.data.get(ATTR_SERIAL))
        door = call.data[ATTR_DOOR]
        try:
            await controller.open_door(door)
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_discover(call: ServiceCall) -> ServiceResponse:
        try:
            found = await discover(
                broadcast_address=call.data[CONF_BROADCAST_ADDRESS],
                timeout=call.data[CONF_TIMEOUT],
            )
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        return {"controllers": [c.as_dict() for c in found]}

    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR, _handle_open_door, schema=OPEN_DOOR_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER,
        _handle_discover,
        schema=DISCOVER_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

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
from homeassistant.util import dt as dt_util

from .api import (
    EventListener,
    UhppoteController,
    UhppoteError,
    discover,
    doors_for_serial,
    local_ip_for,
)
from .const import (
    ATTR_DELAY,
    ATTR_DOOR,
    ATTR_MODE,
    ATTR_SERIAL,
    CONF_BROADCAST_ADDRESS,
    CONF_DOORS,
    CONF_PUSH_ENABLED,
    CONF_PUSH_INTERVAL,
    CONF_PUSH_PORT,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_TIMEOUT,
    DEFAULT_BROADCAST_ADDRESS,
    DEFAULT_PORT,
    DEFAULT_PUSH_ENABLED,
    DEFAULT_PUSH_INTERVAL,
    DEFAULT_PUSH_PORT,
    DEFAULT_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MODE_TO_CODE,
    SERVICE_DISCOVER,
    SERVICE_OPEN_DOOR,
    SERVICE_SET_DOOR_MODE,
    SERVICE_SYNC_TIME,
)
from .coordinator import UhppoteCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.SENSOR,
]

DATA_LISTENER = f"{DOMAIN}_listener"

OPEN_DOOR_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SERIAL): vol.Coerce(int),
        vol.Required(ATTR_DOOR): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
    }
)

SET_DOOR_MODE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SERIAL): vol.Coerce(int),
        vol.Required(ATTR_DOOR): vol.All(vol.Coerce(int), vol.Range(min=1, max=4)),
        vol.Required(ATTR_MODE): vol.In(sorted(MODE_TO_CODE)),
        vol.Optional(ATTR_DELAY): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    }
)

SYNC_TIME_SCHEMA = vol.Schema({vol.Optional(ATTR_SERIAL): vol.Coerce(int)})

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


def _poll_interval(entry: ConfigEntry) -> int:
    """Return the polling interval, widened into a watchdog when pushing.

    With a heartbeat configured, the controller is expected to talk on its own;
    polling only has to notice that it stopped. Without a heartbeat (interval
    0, records only) the regular scan interval is kept.
    """
    scan = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    if not entry.options.get(CONF_PUSH_ENABLED, DEFAULT_PUSH_ENABLED):
        return scan
    heartbeat = entry.options.get(CONF_PUSH_INTERVAL, DEFAULT_PUSH_INTERVAL)
    if heartbeat <= 0:
        return scan
    return max(scan, heartbeat * 3)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a controller."""
    controller = _build_controller(entry)
    doors = (
        entry.options.get(CONF_DOORS)
        or entry.data.get(CONF_DOORS)
        or doors_for_serial(controller.serial)
    )

    coordinator = UhppoteCoordinator(
        hass,
        entry,
        controller,
        doors=doors,
        scan_interval=_poll_interval(entry),
    )
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_load_door_configs()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if entry.options.get(CONF_PUSH_ENABLED, DEFAULT_PUSH_ENABLED):
        await _async_start_push(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a controller."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        return False

    entries: dict = hass.data.get(DOMAIN, {})
    coordinator: UhppoteCoordinator | None = entries.pop(entry.entry_id, None)
    if coordinator is not None and entry.options.get(
        CONF_PUSH_ENABLED, DEFAULT_PUSH_ENABLED
    ):
        await _async_stop_push(hass, entry, coordinator)

    if not entries:
        hass.data.pop(DOMAIN, None)

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# --------------------------------------------------------------------------
# Push channel (function 0x90)
# --------------------------------------------------------------------------


async def _async_start_push(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: UhppoteCoordinator
) -> None:
    """Register Home Assistant as the controller's receiving server.

    Failures are not fatal: the coordinator keeps polling, and the log explains
    why the push channel is unavailable.
    """
    port = entry.options.get(CONF_PUSH_PORT, DEFAULT_PUSH_PORT)
    interval = entry.options.get(CONF_PUSH_INTERVAL, DEFAULT_PUSH_INTERVAL)
    controller = coordinator.controller

    listener: EventListener | None = hass.data.get(DATA_LISTENER)
    if listener is not None and listener.port != port:
        _LOGGER.warning(
            "Controller %s asks for event port %s but port %s is already in use "
            "by another entry; reusing port %s",
            controller.serial,
            port,
            listener.port,
            listener.port,
        )
        port = listener.port

    if listener is None:
        listener = EventListener(port)
        try:
            await listener.async_start()
        except UhppoteError as err:
            _LOGGER.error(
                "Push disabled for controller %s: %s. If Home Assistant runs in "
                "a container, UDP port %s must be published to the host.",
                controller.serial,
                err,
                port,
            )
            return
        hass.data[DATA_LISTENER] = listener

    local_ip = await hass.async_add_executor_job(local_ip_for, controller.host)
    if local_ip is None:
        _LOGGER.error(
            "Push disabled for controller %s: unable to determine which local "
            "address reaches %s",
            controller.serial,
            controller.host,
        )
        return

    try:
        current = await controller.get_receiver()
        if current.enabled and (
            current.ip_address != local_ip or current.port != port
        ):
            _LOGGER.warning(
                "Controller %s was pushing records to %s:%s; Home Assistant is "
                "taking over. A controller stores a single receiving server, so "
                "any other software using it will stop receiving events.",
                controller.serial,
                current.ip_address,
                current.port,
            )
        await controller.set_receiver(local_ip, port, interval)
    except UhppoteError as err:
        _LOGGER.error(
            "Unable to register Home Assistant as the receiving server of "
            "controller %s: %s",
            controller.serial,
            err,
        )
        return

    listener.register(controller.serial, coordinator.handle_push)
    _LOGGER.info(
        "Controller %s will push records to %s:%s (heartbeat %ss)",
        controller.serial,
        local_ip,
        port,
        interval,
    )


async def _async_stop_push(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: UhppoteCoordinator
) -> None:
    """Deregister Home Assistant from the controller, then free the socket."""
    listener: EventListener | None = hass.data.get(DATA_LISTENER)
    controller = coordinator.controller

    if listener is not None:
        listener.unregister(controller.serial)

    try:
        current = await controller.get_receiver()
    except UhppoteError as err:
        _LOGGER.debug(
            "Unable to read the receiving server of controller %s: %s",
            controller.serial,
            err,
        )
        current = None

    # Only clear the setting if it still points at us, so that a controller
    # handed back to vendor software is left alone.
    if current is not None and listener is not None and current.port == listener.port:
        try:
            await controller.set_receiver("0.0.0.0", 0, 0)
        except UhppoteError as err:
            _LOGGER.debug(
                "Unable to clear the receiving server of controller %s: %s",
                controller.serial,
                err,
            )

    if listener is not None and not listener.callbacks:
        await listener.async_stop()
        hass.data.pop(DATA_LISTENER, None)


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------


def _resolve_coordinator(
    hass: HomeAssistant, serial: int | None
) -> UhppoteCoordinator:
    """Resolve the controller targeted by a service call."""
    coordinators: list[UhppoteCoordinator] = list(hass.data.get(DOMAIN, {}).values())

    if not coordinators:
        raise ServiceValidationError("No UHPPOTE controller is configured.")

    if serial is None:
        if len(coordinators) > 1:
            raise ServiceValidationError(
                "Multiple controllers are configured; specify the serial parameter."
            )
        return coordinators[0]

    for coordinator in coordinators:
        if coordinator.serial == serial:
            return coordinator

    known = ", ".join(str(c.serial) for c in coordinators)
    raise ServiceValidationError(
        f"No configured controller has serial number {serial} "
        f"(known serial numbers: {known})."
    )


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services once."""
    if hass.services.has_service(DOMAIN, SERVICE_OPEN_DOOR):
        return

    async def _handle_open_door(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_SERIAL))
        try:
            await coordinator.controller.open_door(call.data[ATTR_DOOR])
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        await coordinator.async_request_refresh()

    async def _handle_set_door_mode(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_SERIAL))
        door = call.data[ATTR_DOOR]
        try:
            if (delay := call.data.get(ATTR_DELAY)) is not None:
                await coordinator.async_set_door_delay(door, delay)
            await coordinator.async_set_door_mode(
                door, MODE_TO_CODE[call.data[ATTR_MODE]]
            )
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err

    async def _handle_sync_time(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call.data.get(ATTR_SERIAL))
        try:
            await coordinator.controller.set_time(dt_util.now().replace(tzinfo=None))
        except UhppoteError as err:
            raise HomeAssistantError(str(err)) from err
        await coordinator.async_request_refresh()

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
        SERVICE_SET_DOOR_MODE,
        _handle_set_door_mode,
        schema=SET_DOOR_MODE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_TIME, _handle_sync_time, schema=SYNC_TIME_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DISCOVER,
        _handle_discover,
        schema=DISCOVER_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

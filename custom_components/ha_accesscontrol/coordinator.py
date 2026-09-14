"""Polling and push coordinator for UHPPOTE controllers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ControllerStatus, DoorConfig, UhppoteController, UhppoteError
from .const import (
    CODE_TO_MODE,
    DOMAIN,
    EVENT_RECORD,
    RECORD_TYPES,
)

_LOGGER = logging.getLogger(__name__)


def as_local(moment: datetime | None) -> datetime | None:
    """Attach Home Assistant's timezone to a naive controller timestamp.

    The controller reports wall-clock time with no timezone information, so it
    is interpreted as local time.
    """
    if moment is None:
        return None
    return moment.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)


class UhppoteCoordinator(DataUpdateCoordinator[ControllerStatus]):
    """Keep one controller's state up to date, by polling and/or push.

    When the controller pushes a record, :meth:`handle_push` feeds it straight
    into the coordinator. That also reschedules the next poll, so a silent
    controller is detected automatically once the fallback interval elapses.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        controller: UhppoteController,
        *,
        doors: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {controller.serial}",
            update_interval=timedelta(seconds=scan_interval),
            config_entry=entry,
        )
        self.controller = controller
        self.doors = doors
        self.door_configs: dict[int, DoorConfig] = {}
        self._last_record_index: int | None = None
        self._push_active = False

    @property
    def serial(self) -> int:
        return self.controller.serial

    @property
    def push_active(self) -> bool:
        """Whether at least one record has arrived over the push channel."""
        return self._push_active

    # ------------------------------------------------------------------ poll

    async def _async_update_data(self) -> ControllerStatus:
        try:
            status = await self.controller.get_status()
        except UhppoteError as err:
            raise UpdateFailed(str(err)) from err

        self._process(status)
        return status

    # ------------------------------------------------------------------ push

    @callback
    def handle_push(self, status: ControllerStatus) -> None:
        """Accept a record pushed by the controller (function 0x90)."""
        self._push_active = True
        self._process(status)
        self.async_set_updated_data(status)

    # --------------------------------------------------------------- events

    @callback
    def _process(self, status: ControllerStatus) -> None:
        """Fire a bus event when the controller reports a new record."""
        previous, self._last_record_index = (
            self._last_record_index,
            status.last_record_index,
        )

        if previous is None or status.last_record_index == previous:
            return
        if not status.has_event:
            return

        event_time = as_local(status.event_time)
        self.hass.bus.async_fire(
            EVENT_RECORD,
            {
                "serial": status.serial,
                "index": status.last_record_index,
                "record_type": RECORD_TYPES.get(status.record_type, "unknown"),
                "granted": status.granted,
                "door": status.door,
                "direction": "in" if status.direction == 1 else "out",
                "card_number": status.card_number,
                "reason_code": status.reason_code,
                "timestamp": event_time.isoformat() if event_time else None,
            },
        )

    # ---------------------------------------------------------- door config

    async def async_load_door_configs(self) -> None:
        """Read every door's control mode and delay (function 0x82).

        Failures are tolerated: the lock entities fall back to "unknown" rather
        than preventing the whole entry from loading.
        """
        for door in range(1, self.doors + 1):
            try:
                self.door_configs[door] = await self.controller.get_door_config(door)
            except UhppoteError as err:
                _LOGGER.warning(
                    "Unable to read the configuration of door %s on controller %s: %s",
                    door,
                    self.serial,
                    err,
                )

    async def async_set_door_mode(self, door: int, mode: int) -> None:
        """Change a door's control mode, preserving its open delay.

        Function 0x80 writes the mode and the delay in a single packet, so the
        cached delay must be sent back or it would be overwritten.
        """
        known = self.door_configs.get(door)
        if known is None:
            known = await self.controller.get_door_config(door)

        updated = await self.controller.set_door_config(door, mode, known.delay)
        self.door_configs[door] = updated
        _LOGGER.debug(
            "Door %s on controller %s switched to mode %s",
            door,
            self.serial,
            CODE_TO_MODE.get(updated.mode, updated.mode),
        )
        await self.async_request_refresh()

    async def async_set_door_delay(self, door: int, delay: int) -> None:
        """Change a door's open delay, preserving its control mode."""
        known = self.door_configs.get(door)
        if known is None:
            known = await self.controller.get_door_config(door)

        updated = await self.controller.set_door_config(door, known.mode, delay)
        self.door_configs[door] = updated
        await self.async_request_refresh()

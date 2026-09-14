"""Polling and push coordinator for UHPPOTE controllers."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DOOR_MODE_CONTROLLED,
    ControllerStatus,
    DoorConfig,
    UhppoteController,
    UhppoteError,
)
from .const import (
    CLOCK_DRIFT_THRESHOLD,
    CODE_TO_MODE,
    DEFAULT_OPEN_SOURCE,
    DOMAIN,
    EVENT_RECORD,
    RECORD_TYPES,
)
from .timezones import is_dst, next_utc_offset_change

_LOGGER = logging.getLogger(__name__)

# Fire just after the transition, never exactly on it.
CLOCK_SYNC_MARGIN = timedelta(seconds=5)
# A controller that was unreachable at the transition must not wait six months
# for the next one.
CLOCK_SYNC_RETRY = timedelta(minutes=10)


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
        sync_clock: bool = False,
        open_source: str = DEFAULT_OPEN_SOURCE,
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
        # Open delays taken from a reading made in online control mode, which
        # is the only mode in which the controller reports a meaningful one.
        self._delays: dict[int, int] = {}
        self._last_record_index: int | None = None
        self._push_active = False
        self._pulse_timers: dict[int, CALLBACK_TYPE] = {}
        self.sync_clock = sync_clock
        self.open_source = open_source
        self.next_clock_sync: datetime | None = None
        self._clock_timer: CALLBACK_TYPE | None = None

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

    def _remember(self, config: DoorConfig) -> DoorConfig:
        """Cache a door configuration, keeping its open delay trustworthy.

        The delay byte only carries a meaning in online control mode. A door
        held normally open is not released on a delay at all, and a controller
        in that mode reports the field as zero -- so taking it at face value,
        and later sending it back alongside a new mode, silently destroys the
        configured pulse length.

        The delay is therefore only learned from a reading made in online
        mode. Any other reading keeps the last known good value, and only a
        door never yet seen online falls back to what the controller says.
        """
        if config.mode == DOOR_MODE_CONTROLLED:
            self._delays[config.door] = config.delay

        delay = self._delays.get(config.door, config.delay)
        stored = config if delay == config.delay else replace(config, delay=delay)
        self.door_configs[config.door] = stored
        return stored

    async def async_load_door_configs(self) -> None:
        """Read every door's control mode and delay (function 0x82).

        Failures are tolerated: the lock entities fall back to "unknown" rather
        than preventing the whole entry from loading.
        """
        for door in range(1, self.doors + 1):
            try:
                self._remember(await self.controller.get_door_config(door))
            except UhppoteError as err:
                _LOGGER.warning(
                    "Unable to read the configuration of door %s on controller %s: %s",
                    door,
                    self.serial,
                    err,
                )

    async def async_set_door_mode(self, door: int, mode: int) -> None:
        """Change a door's control mode, preserving its open delay.

        Function 0x80 writes the mode and the delay in a single packet, so a
        delay has to be sent along or it would be overwritten. It comes from
        the cache rather than from a fresh reading: the door may currently be
        held normally open, and the controller reports no usable delay then.
        Reading it back would zero the configuration the moment the door is
        returned to online control.
        """
        delay = self.open_delay(door)
        if delay is None:
            # A door that has never been read: nothing better to send back.
            delay = (await self.controller.get_door_config(door)).delay

        updated = self._remember(
            await self.controller.set_door_config(door, mode, delay)
        )
        _LOGGER.debug(
            "Door %s on controller %s switched to mode %s",
            door,
            self.serial,
            CODE_TO_MODE.get(updated.mode, updated.mode),
        )
        await self.async_request_refresh()

    async def async_set_door_delay(self, door: int, delay: int) -> None:
        """Change a door's open delay, preserving its control mode."""
        current = await self.controller.get_door_config(door)
        updated = await self.controller.set_door_config(door, current.mode, delay)
        # An explicit instruction, so it is trusted whatever the current mode:
        # setting the delay of a door held open must survive it being closed.
        self._delays[door] = delay
        self._remember(updated)
        await self.async_request_refresh()

    def open_delay(self, door: int) -> int | None:
        """Return the cached open delay of a door, in seconds."""
        config = self.door_configs.get(door)
        return None if config is None else config.delay

    # ---------------------------------------------------------- open pulse

    @callback
    def async_schedule_pulse_refresh(self, door: int) -> None:
        """Refresh once an open pulse has elapsed.

        A remote open releases the relay for the door's configured delay. Now
        that the delay is known, the relay sensor and the lock state can settle
        as soon as it falls back, instead of waiting for the next poll.
        """
        delay = self.open_delay(door)
        if delay is None:
            return

        if (cancel := self._pulse_timers.pop(door, None)) is not None:
            cancel()

        async def _settled(_now) -> None:
            self._pulse_timers.pop(door, None)
            # Not async_request_refresh: its debouncer would swallow this one
            # right after the refresh that followed the open command.
            await self.async_refresh()

        self._pulse_timers[door] = async_call_later(self.hass, delay + 1, _settled)

    # ----------------------------------------------------------- the clock

    @property
    def dst_active(self) -> bool:
        """Whether daylight saving is currently in effect where the user is."""
        return is_dst(dt_util.DEFAULT_TIME_ZONE, dt_util.utcnow())

    def clock_drift(self) -> float | None:
        """Seconds the controller clock is ahead of Home Assistant.

        ``None`` on firmware that does not report its current date, which
        cannot be compared reliably across midnight.
        """
        if self.data is None or self.data.controller_time is None:
            return None
        moment = as_local(self.data.controller_time)
        return (moment - dt_util.now()).total_seconds()

    async def async_sync_clock(self) -> None:
        """Write Home Assistant's local wall clock to the controller (0x30)."""
        await self.controller.set_time(dt_util.now().replace(tzinfo=None))
        _LOGGER.info("Clock of controller %s synchronised", self.serial)
        await self.async_request_refresh()

    async def async_sync_clock_if_drifted(self) -> bool:
        """Rewrite the clock when it is measurably wrong.

        This is the recovery path: a transition that happened while Home
        Assistant was down leaves the controller an hour out, and nothing else
        would notice until the next one.
        """
        drift = self.clock_drift()
        if drift is None or abs(drift) < CLOCK_DRIFT_THRESHOLD:
            return False

        _LOGGER.info(
            "Clock of controller %s is off by %.0f s; resynchronising",
            self.serial,
            drift,
        )
        await self.async_sync_clock()
        return True

    @callback
    def async_schedule_clock_sync(self) -> None:
        """Arrange a resync at the next change of the local UTC offset.

        The controllers have no daylight-saving rules, so the clock has to be
        rewritten at every transition. Rescheduling happens after each run, and
        whenever Home Assistant's timezone changes.
        """
        if self._clock_timer is not None:
            self._clock_timer()
            self._clock_timer = None

        self.next_clock_sync = None
        if not self.sync_clock:
            return

        timezone = dt_util.DEFAULT_TIME_ZONE
        moment = next_utc_offset_change(timezone, dt_util.utcnow())
        if moment is None:
            _LOGGER.debug(
                "Timezone %s has no upcoming offset change; controller %s needs "
                "no scheduled resynchronisation",
                timezone,
                self.serial,
            )
            return

        self.next_clock_sync = moment
        self._clock_timer = async_track_point_in_utc_time(
            self.hass, self._async_clock_transition, moment + CLOCK_SYNC_MARGIN
        )
        _LOGGER.debug(
            "Clock of controller %s will be resynchronised at %s",
            self.serial,
            moment.isoformat(),
        )

    async def _async_clock_transition(self, _now: datetime) -> None:
        """Run at a daylight-saving transition, then arm the next one."""
        self._clock_timer = None
        try:
            await self.async_sync_clock()
        except UhppoteError as err:
            _LOGGER.warning(
                "Could not resynchronise the clock of controller %s at the "
                "daylight-saving transition: %s. Retrying in %s minutes.",
                self.serial,
                err,
                int(CLOCK_SYNC_RETRY.total_seconds() // 60),
            )
            self._clock_timer = async_track_point_in_utc_time(
                self.hass,
                self._async_clock_transition,
                dt_util.utcnow() + CLOCK_SYNC_RETRY,
            )
            return

        self.async_schedule_clock_sync()

    async def async_shutdown(self) -> None:
        """Cancel pending timers before the coordinator goes away."""
        for cancel in self._pulse_timers.values():
            cancel()
        self._pulse_timers.clear()
        if self._clock_timer is not None:
            self._clock_timer()
            self._clock_timer = None
        await super().async_shutdown()

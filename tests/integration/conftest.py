"""Fixtures driving a real Home Assistant instance.

These tests need the 'integration' dependency group; the parent conftest skips
this whole directory when pytest-homeassistant-custom-component is missing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ha_accesscontrol.api import (
    DOOR_MODE_CONTROLLED,
    RECORD_CARD,
    ControllerStatus,
    DoorConfig,
    ReceiverConfig,
    UhppoteTimeout,
)
from custom_components.ha_accesscontrol.const import (
    CONF_DOORS,
    CONF_SERIAL,
    DOMAIN,
)

SERIAL = 223000123  # a two-door controller
HOST = "192.168.1.50"
LOCAL_IP = "192.168.1.10"

# The controller reports wall-clock time with no timezone, so this is what the
# integration must interpret as local time. Home Assistant's test harness runs
# in US/Pacific, which keeps these assertions honest.
EVENT_WALL_CLOCK = datetime(2015, 4, 29, 16, 37, 39)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/ during the tests."""
    return


def make_status(**overrides: Any) -> ControllerStatus:
    """Build a status packet with sane defaults, overridable per test."""
    defaults: dict[str, Any] = {
        "serial": SERIAL,
        "last_record_index": 1,
        "record_type": RECORD_CARD,
        "granted": True,
        "door": 1,
        "direction": 1,
        "card_number": 3659533,
        "event_time": EVENT_WALL_CLOCK,
        "reason_code": 1,
        "door_sensors": (False, False, False, False),
        "buttons": (False, False, False, False),
        "error_code": 0,
        "relays": (False, False, False, False),
        "forced_lock": False,
        "fire": False,
        "controller_time": EVENT_WALL_CLOCK,
    }
    return ControllerStatus(**{**defaults, **overrides})


class StubController:
    """In-memory stand-in for UhppoteController.

    It records what the integration sent and lets a test inject failures,
    without going near a socket.
    """

    def __init__(self, host: str = HOST, serial: int = SERIAL, **kwargs: Any) -> None:
        self.host = host
        self.serial = int(serial)
        self.port = kwargs.get("port", 60000)
        self.timeout = kwargs.get("timeout")
        self.retries = kwargs.get("retries")

        self.status = make_status()
        self.door_configs: dict[int, DoorConfig] = {
            door: DoorConfig(door=door, mode=DOOR_MODE_CONTROLLED, delay=3)
            for door in (1, 2, 3, 4)
        }
        self.receiver = ReceiverConfig(ip_address="0.0.0.0", port=0, interval=0)

        # Recorded calls, inspected by the tests.
        self.opened: list[int] = []
        self.status_calls = 0
        self.receiver_writes: list[tuple[str, int, int]] = []
        self.time_writes: list[datetime] = []

        # Failure injection.
        self.status_error: Exception | None = None
        self.open_error: Exception | None = None
        self.receiver_error: Exception | None = None

    @property
    def doors(self) -> int:
        return 2

    async def get_status(self) -> ControllerStatus:
        self.status_calls += 1
        if self.status_error is not None:
            raise self.status_error
        return self.status

    async def open_door(self, door: int) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened.append(door)

    async def get_door_config(self, door: int) -> DoorConfig:
        if door not in self.door_configs:
            # What a real controller does for a door this model does not have:
            # it simply never answers.
            raise UhppoteTimeout(f"no answer for door {door}")
        return self.door_configs[door]

    async def set_door_config(self, door: int, mode: int, delay: int) -> DoorConfig:
        self.door_configs[door] = DoorConfig(door=door, mode=mode, delay=delay)
        return self.door_configs[door]

    async def get_receiver(self) -> ReceiverConfig:
        if self.receiver_error is not None:
            raise self.receiver_error
        return self.receiver

    async def set_receiver(self, ip_address: str, port: int, interval: int) -> None:
        if self.receiver_error is not None:
            raise self.receiver_error
        self.receiver_writes.append((ip_address, port, interval))
        self.receiver = ReceiverConfig(
            ip_address=ip_address, port=port, interval=interval
        )

    async def set_time(self, moment: datetime) -> datetime:
        self.time_writes.append(moment)
        return moment


@pytest.fixture
def controller() -> StubController:
    """The stub the integration will talk to."""
    return StubController()


@pytest.fixture
def listener() -> MagicMock:
    """A stand-in for the push listener, with its registrations recorded."""
    instance = MagicMock()
    instance.port = 60002
    instance.bound_port = 60002
    instance.callbacks = {}
    instance.async_start = AsyncMock()
    instance.async_stop = AsyncMock()
    instance.register = MagicMock(
        side_effect=lambda serial, callback: instance.callbacks.__setitem__(
            serial, callback
        )
    )
    instance.unregister = MagicMock(
        side_effect=lambda serial: instance.callbacks.pop(serial, None)
    )
    return instance


@pytest.fixture
def patched(controller: StubController, listener: MagicMock):
    """Patch every outbound dependency of the integration."""
    with (
        patch(
            "custom_components.ha_accesscontrol.UhppoteController",
            return_value=controller,
        ),
        patch(
            "custom_components.ha_accesscontrol.EventListener",
            return_value=listener,
        ),
        patch(
            "custom_components.ha_accesscontrol.local_ip_for",
            return_value=LOCAL_IP,
        ),
    ):
        yield controller


def build_entry(**options: Any) -> MockConfigEntry:
    """A config entry for the stub controller."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=str(SERIAL),
        title=f"Controller {SERIAL}",
        data={
            CONF_HOST: HOST,
            CONF_SERIAL: SERIAL,
            CONF_PORT: 60000,
            CONF_DOORS: 2,
        },
        options=options,
    )


@pytest.fixture
async def setup_entry(hass: HomeAssistant, patched) -> Callable[..., Any]:
    """Return a helper setting up the integration with given options."""

    async def _setup(**options: Any) -> MockConfigEntry:
        entry = build_entry(**options)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


@pytest.fixture
async def entry(setup_entry) -> MockConfigEntry:
    """The integration set up with its default options (no push)."""
    return await setup_entry()


@pytest.fixture
def entity_id_for(hass: HomeAssistant) -> Callable[[str, str], str]:
    """Resolve an entity id from its unique id, rather than guessing names."""

    def _lookup(platform: str, unique_id: str) -> str:
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(platform, DOMAIN, unique_id)
        assert entity_id is not None, f"no {platform} entity with unique id {unique_id}"
        return entity_id

    return _lookup


@pytest.fixture
def timeout_error() -> Exception:
    return UhppoteTimeout("controller did not answer")

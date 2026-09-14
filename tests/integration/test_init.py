"""Entry setup and teardown, including the push channel."""

from __future__ import annotations

import logging

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.ha_accesscontrol.api import UhppoteRefused, UhppoteTimeout
from custom_components.ha_accesscontrol.const import DOMAIN

from .conftest import LOCAL_IP, SERIAL


async def test_setup_creates_the_device_and_its_entities(
    hass: HomeAssistant, entry
) -> None:
    assert entry.state is ConfigEntryState.LOADED

    device = dr.async_get(hass).async_get_device({(DOMAIN, str(SERIAL))})
    assert device is not None
    assert device.serial_number == str(SERIAL)

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    by_platform: dict[str, int] = {}
    for item in entities:
        by_platform[item.domain] = by_platform.get(item.domain, 0) + 1

    # Two doors: 3 binary sensors + 1 button + 1 lock + 1 number per door,
    # plus 3 controller-level binary sensors, 7 sensors and the clock button.
    assert by_platform == {
        "binary_sensor": 2 * 3 + 3,
        "button": 2 + 1,
        "lock": 2,
        "number": 2,
        "sensor": 7,
    }


async def test_unload_removes_everything(hass: HomeAssistant, entry) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_schedule_pulse_refresh(1)
    assert coordinator._pulse_timers

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not coordinator._pulse_timers
    assert DOMAIN not in hass.data


async def test_setup_fails_when_the_controller_is_silent(
    hass: HomeAssistant, setup_entry, controller, timeout_error
) -> None:
    controller.status_error = timeout_error

    from .conftest import build_entry

    entry = build_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_services_are_registered(hass: HomeAssistant, entry) -> None:
    for service in ("open_door", "set_door_mode", "sync_time", "discover"):
        assert hass.services.has_service(DOMAIN, service), service


# ------------------------------------------------------------------ push


async def test_push_is_off_by_default(
    hass: HomeAssistant, entry, controller, listener
) -> None:
    """A controller stores a single receiving server; never take it silently."""
    assert controller.receiver_writes == []
    listener.async_start.assert_not_called()


async def test_push_registers_home_assistant(
    hass: HomeAssistant, setup_entry, controller, listener
) -> None:
    await setup_entry(push_enabled=True, push_port=60002, push_interval=15)

    listener.async_start.assert_awaited_once()
    assert controller.receiver_writes == [(LOCAL_IP, 60002, 15)]
    assert SERIAL in listener.callbacks


async def test_push_warns_before_replacing_another_receiver(
    hass: HomeAssistant,
    setup_entry,
    controller,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.ha_accesscontrol.api import ReceiverConfig

    controller.receiver = ReceiverConfig(
        ip_address="192.168.1.200", port=61005, interval=5
    )

    with caplog.at_level(logging.WARNING):
        await setup_entry(push_enabled=True, push_port=60002, push_interval=15)

    assert "192.168.1.200" in caplog.text
    assert "single receiving server" in caplog.text


async def test_push_failure_does_not_break_the_entry(
    hass: HomeAssistant,
    setup_entry,
    controller,
    listener,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A controller that refuses 0x90 must still be usable by polling."""
    controller.receiver_error = UhppoteRefused("refused")

    with caplog.at_level(logging.ERROR):
        entry = await setup_entry(push_enabled=True, push_port=60002)

    assert entry.state is ConfigEntryState.LOADED
    assert listener.callbacks == {}, "no record can arrive, so nothing is routed"
    assert "receiving server" in caplog.text


async def test_unload_releases_the_push_channel(
    hass: HomeAssistant, setup_entry, controller, listener
) -> None:
    entry = await setup_entry(push_enabled=True, push_port=60002, push_interval=15)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    listener.unregister.assert_called_once_with(SERIAL)
    assert controller.receiver_writes[-1] == ("0.0.0.0", 0, 0)
    listener.async_stop.assert_awaited_once()


async def test_unload_leaves_a_foreign_receiver_alone(
    hass: HomeAssistant, setup_entry, controller, listener
) -> None:
    """Another system took the slot while we were running: do not clear it."""
    from custom_components.ha_accesscontrol.api import ReceiverConfig

    entry = await setup_entry(push_enabled=True, push_port=60002, push_interval=15)
    controller.receiver = ReceiverConfig(
        ip_address="192.168.1.200", port=61005, interval=5
    )
    writes_before = len(controller.receiver_writes)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert len(controller.receiver_writes) == writes_before, (
        "the other receiver's configuration must be preserved"
    )


async def test_unload_survives_an_unreachable_controller(
    hass: HomeAssistant, setup_entry, controller, listener
) -> None:
    entry = await setup_entry(push_enabled=True, push_port=60002)
    controller.receiver_error = UhppoteTimeout("gone")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# ------------------------------------------------------------- polling


@pytest.mark.parametrize(
    ("options", "expected", "reason"),
    [
        ({}, 10, "default polling"),
        ({"scan_interval": 42}, 42, "explicit polling"),
        (
            {"push_enabled": True, "push_interval": 30},
            90,
            "watchdog: three missed heartbeats",
        ),
        (
            {"push_enabled": True, "push_interval": 0},
            10,
            "no heartbeat, so keep polling normally",
        ),
        (
            {"push_enabled": True, "push_interval": 1, "scan_interval": 20},
            20,
            "never poll less often than asked",
        ),
    ],
)
async def test_poll_interval(
    hass: HomeAssistant, setup_entry, options, expected: int, reason: str
) -> None:
    entry = await setup_entry(**options)

    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.update_interval.total_seconds() == expected, reason

"""The configuration and options flows."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha_accesscontrol.api import ControllerInfo, UhppoteError
from custom_components.ha_accesscontrol.const import (
    CONF_DOORS,
    CONF_PUSH_ENABLED,
    CONF_PUSH_INTERVAL,
    CONF_PUSH_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    DOMAIN,
)

from .conftest import HOST, SERIAL, build_entry

FOUND = ControllerInfo(
    serial=SERIAL,
    ip_address=HOST,
    netmask="255.255.255.0",
    gateway="192.168.1.1",
    mac_address="00:66:19:39:55:2d",
    version="6.56",
    release_date="2019-08-29",
)


def _doors_selector(result):
    """The selector object behind the doors field of a form result."""
    for key, value in result["data_schema"].schema.items():
        if key == CONF_DOORS:
            return value
    raise AssertionError("the form has no doors field")


def _preselected_doors(result) -> str:
    for key in result["data_schema"].schema:
        if key == CONF_DOORS:
            return key.default()
    raise AssertionError("the form has no doors field")


async def _start(hass: HomeAssistant, step: str):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": step}
    )


# ------------------------------------------------------------------ manual


async def test_manual_asks_for_the_door_count(hass: HomeAssistant) -> None:
    """A serial starting with 2 is a two-door controller, so 2 is preselected."""
    result = await _start(hass, "manual")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: HOST, CONF_SERIAL: SERIAL, CONF_PORT: 60000},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "doors"
    assert result["description_placeholders"] == {"serial": str(SERIAL)}
    assert _preselected_doors(result) == "2"

    with patch(
        "custom_components.ha_accesscontrol.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DOORS: "4"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Controller {SERIAL}"
    assert result["data"] == {
        CONF_HOST: HOST,
        CONF_SERIAL: SERIAL,
        CONF_PORT: 60000,
        CONF_DOORS: 4,
    }, "the chosen count wins over the one derived from the serial"


@pytest.mark.parametrize(
    ("serial", "preselected"),
    [(123000123, "1"), (223000123, "2"), (423000123, "4"), (923000123, "4")],
)
async def test_door_count_preselection(
    hass: HomeAssistant, serial: int, preselected: str
) -> None:
    result = await _start(hass, "manual")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_SERIAL: serial}
    )

    assert _preselected_doors(result) == preselected


async def test_door_count_offers_four_radio_options(hass: HomeAssistant) -> None:
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_SERIAL: SERIAL}
    )

    config = _doors_selector(result).config

    assert config["options"] == ["1", "2", "3", "4"]
    assert config["mode"] == "list", "list mode is what renders radio buttons"
    assert config["translation_key"] == "doors"


async def test_door_count_rejects_anything_else(hass: HomeAssistant) -> None:
    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: HOST, CONF_SERIAL: SERIAL}
    )

    with pytest.raises(vol.Invalid):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DOORS: "5"}
        )


async def test_manual_rejects_a_duplicate(hass: HomeAssistant) -> None:
    build_entry().add_to_hass(hass)

    result = await _start(hass, "manual")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.168.1.99", CONF_SERIAL: SERIAL}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --------------------------------------------------------------- discovery


async def test_discovery_creates_an_entry(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.ha_accesscontrol.config_flow.discover",
        return_value=[FOUND],
    ):
        result = await _start(hass, "discovery")

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SERIAL: str(SERIAL)}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "doors"
    assert _preselected_doors(result) == "2"

    with patch(
        "custom_components.ha_accesscontrol.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_DOORS: "2"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST
    assert result["data"][CONF_DOORS] == 2


async def test_discovery_aborts_when_nothing_answers(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.ha_accesscontrol.config_flow.discover", return_value=[]
    ):
        result = await _start(hass, "discovery")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_discovery_aborts_when_the_broadcast_fails(
    hass: HomeAssistant,
) -> None:
    with patch(
        "custom_components.ha_accesscontrol.config_flow.discover",
        side_effect=UhppoteError("network unreachable"),
    ):
        result = await _start(hass, "discovery")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_discovery_hides_already_configured_controllers(
    hass: HomeAssistant,
) -> None:
    build_entry().add_to_hass(hass)

    with patch(
        "custom_components.ha_accesscontrol.config_flow.discover",
        return_value=[FOUND],
    ):
        result = await _start(hass, "discovery")

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


# ----------------------------------------------------------------- options


async def test_options_flow(hass: HomeAssistant, entry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "timeout": 4.0,
            "retries": 1,
            CONF_SCAN_INTERVAL: 25,
            CONF_DOORS: "4",
            CONF_PUSH_ENABLED: True,
            CONF_PUSH_PORT: 60123,
            CONF_PUSH_INTERVAL: 20,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PUSH_PORT] == 60123
    assert entry.options[CONF_DOORS] == 4


async def test_options_change_reloads_the_entry(
    hass: HomeAssistant, entry, controller
) -> None:
    """The door count override must take effect without a restart."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "timeout": 2.5,
            "retries": 2,
            CONF_SCAN_INTERVAL: 10,
            CONF_DOORS: "4",
            CONF_PUSH_ENABLED: False,
            CONF_PUSH_PORT: 60002,
            CONF_PUSH_INTERVAL: 30,
        },
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.doors == 4

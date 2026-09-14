"""Configuration flow for the UHPPOTE integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector

from .api import ControllerInfo, UhppoteError, discover, doors_for_serial
from .const import (
    CONF_DOORS,
    CONF_PUSH_ENABLED,
    CONF_PUSH_INTERVAL,
    CONF_PUSH_PORT,
    CONF_RETRIES,
    CONF_SCAN_INTERVAL,
    CONF_SERIAL,
    CONF_SYNC_CLOCK,
    CONF_TIMEOUT,
    DEFAULT_BROADCAST_ADDRESS,
    DEFAULT_PORT,
    DEFAULT_PUSH_ENABLED,
    DEFAULT_PUSH_INTERVAL,
    DEFAULT_PUSH_PORT,
    DEFAULT_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SYNC_CLOCK,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_SERIAL): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    }
)

# Radio buttons rather than a number box: there are only four possible answers,
# and the model is usually known from the serial number anyway.
DOORS_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=["1", "2", "3", "4"],
        mode=selector.SelectSelectorMode.LIST,
        translation_key="doors",
    )
)


def _doors_schema(default: int) -> vol.Schema:
    """Radio buttons for the door count, preselected on ``default``."""
    return vol.Schema({vol.Required(CONF_DOORS, default=str(default)): DOORS_SELECTOR})


class UhppoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a controller through discovery or manual entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, ControllerInfo] = {}
        self._pending: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose between discovery and manual entry."""
        return self.async_show_menu(
            step_id="user", menu_options=["discovery", "manual"]
        )

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover controllers by broadcast (function 0x94)."""
        if user_input is None:
            try:
                found = await discover(
                    broadcast_address=DEFAULT_BROADCAST_ADDRESS, timeout=3.0
                )
            except UhppoteError as err:
                _LOGGER.warning("Discovery failed: %s", err)
                found = []

            configured = {entry.unique_id for entry in self._async_current_entries()}
            self._discovered = {
                str(info.serial): info
                for info in found
                if str(info.serial) not in configured
            }

            if not self._discovered:
                return self.async_abort(reason="no_devices_found")

            options = {
                serial: f"{serial} — {info.ip_address} (v{info.version})"
                for serial, info in self._discovered.items()
            }
            return self.async_show_form(
                step_id="discovery",
                data_schema=vol.Schema({vol.Required(CONF_SERIAL): vol.In(options)}),
            )

        info = self._discovered[user_input[CONF_SERIAL]]
        await self.async_set_unique_id(str(info.serial))
        self._abort_if_unique_id_configured()

        self._pending = {
            CONF_HOST: info.ip_address,
            CONF_SERIAL: info.serial,
            CONF_PORT: DEFAULT_PORT,
        }
        return await self.async_step_doors()

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manually enter the IP address and serial number."""
        if user_input is None:
            return self.async_show_form(step_id="manual", data_schema=MANUAL_SCHEMA)

        await self.async_set_unique_id(str(user_input[CONF_SERIAL]))
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: user_input[CONF_HOST]}
        )

        self._pending = dict(user_input)
        return await self.async_step_doors()

    async def async_step_doors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how many doors the controller drives.

        The leading digit of the serial number identifies the model, so the
        matching option is preselected and the step is usually one click.
        """
        serial = self._pending[CONF_SERIAL]

        if user_input is not None:
            return self.async_create_entry(
                title=f"Controller {serial}",
                data={**self._pending, CONF_DOORS: int(user_input[CONF_DOORS])},
            )

        return self.async_show_form(
            step_id="doors",
            data_schema=_doors_schema(doors_for_serial(serial)),
            description_placeholders={"serial": str(serial)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return UhppoteOptionsFlow()


class UhppoteOptionsFlow(OptionsFlow):
    """Configure polling, retries, the clock and the push channel."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # The selector hands back a string; everything downstream counts
            # doors with it.
            return self.async_create_entry(
                data={**user_input, CONF_DOORS: int(user_input[CONF_DOORS])}
            )

        entry = self.config_entry
        options = entry.options
        default_doors = (
            options.get(CONF_DOORS)
            or entry.data.get(CONF_DOORS)
            or doors_for_serial(entry.data[CONF_SERIAL])
        )

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_TIMEOUT,
                    default=options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=15)),
                vol.Optional(
                    CONF_RETRIES,
                    default=options.get(CONF_RETRIES, DEFAULT_RETRIES),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=5)),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=3600)),
                vol.Required(CONF_DOORS, default=str(default_doors)): DOORS_SELECTOR,
                vol.Optional(
                    CONF_SYNC_CLOCK,
                    default=options.get(CONF_SYNC_CLOCK, DEFAULT_SYNC_CLOCK),
                ): cv.boolean,
                vol.Optional(
                    CONF_PUSH_ENABLED,
                    default=options.get(CONF_PUSH_ENABLED, DEFAULT_PUSH_ENABLED),
                ): cv.boolean,
                vol.Optional(
                    CONF_PUSH_PORT,
                    default=options.get(CONF_PUSH_PORT, DEFAULT_PUSH_PORT),
                ): cv.port,
                vol.Optional(
                    CONF_PUSH_INTERVAL,
                    default=options.get(CONF_PUSH_INTERVAL, DEFAULT_PUSH_INTERVAL),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

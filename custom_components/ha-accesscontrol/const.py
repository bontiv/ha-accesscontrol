"""Constants for the UHPPOTE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "uhppote"

CONF_SERIAL: Final = "serial"
CONF_DOORS: Final = "doors"
CONF_TIMEOUT: Final = "timeout"
CONF_RETRIES: Final = "retries"
CONF_BROADCAST_ADDRESS: Final = "broadcast_address"

DEFAULT_PORT: Final = 60000
DEFAULT_DOORS: Final = 4
DEFAULT_TIMEOUT: Final = 2.5
DEFAULT_RETRIES: Final = 2
DEFAULT_BROADCAST_ADDRESS: Final = "255.255.255.255"

SERVICE_OPEN_DOOR: Final = "open_door"
SERVICE_DISCOVER: Final = "discover"

ATTR_SERIAL: Final = "serial"
ATTR_DOOR: Final = "door"

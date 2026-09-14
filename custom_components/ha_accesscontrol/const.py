"""Constants for the UHPPOTE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ha_accesscontrol"

CONF_SERIAL: Final = "serial"
CONF_DOORS: Final = "doors"
CONF_TIMEOUT: Final = "timeout"
CONF_RETRIES: Final = "retries"
CONF_BROADCAST_ADDRESS: Final = "broadcast_address"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_PUSH_ENABLED: Final = "push_enabled"
CONF_PUSH_PORT: Final = "push_port"
CONF_PUSH_INTERVAL: Final = "push_interval"

DEFAULT_PORT: Final = 60000
DEFAULT_DOORS: Final = 4
DEFAULT_TIMEOUT: Final = 2.5
DEFAULT_RETRIES: Final = 2
DEFAULT_BROADCAST_ADDRESS: Final = "255.255.255.255"
DEFAULT_SCAN_INTERVAL: Final = 10
# Pushing is an opt-in: a controller stores a single receiving server, so
# enabling it silently replaces whatever vendor software was registered.
DEFAULT_PUSH_ENABLED: Final = False
DEFAULT_PUSH_PORT: Final = 60002
DEFAULT_PUSH_INTERVAL: Final = 30

SERVICE_OPEN_DOOR: Final = "open_door"
SERVICE_DISCOVER: Final = "discover"
SERVICE_SET_DOOR_MODE: Final = "set_door_mode"
SERVICE_SYNC_TIME: Final = "sync_time"

ATTR_SERIAL: Final = "serial"
ATTR_DOOR: Final = "door"
ATTR_MODE: Final = "mode"
ATTR_DELAY: Final = "delay"

EVENT_RECORD: Final = f"{DOMAIN}_event"

# Human-readable door control modes, used by the select-like service field and
# by the lock entity attributes.
MODE_NORMALLY_OPEN: Final = "normally_open"
MODE_NORMALLY_CLOSED: Final = "normally_closed"
MODE_CONTROLLED: Final = "controlled"

MODE_TO_CODE: Final[dict[str, int]] = {
    MODE_NORMALLY_OPEN: 1,
    MODE_NORMALLY_CLOSED: 2,
    MODE_CONTROLLED: 3,
}
CODE_TO_MODE: Final[dict[int, str]] = {v: k for k, v in MODE_TO_CODE.items()}

# Record types reported by function 0x20
RECORD_TYPES: Final[dict[int, str]] = {
    0x00: "none",
    0x01: "card",
    0x02: "door",
    0x03: "alarm",
    0xFF: "overwritten",
}

"""UDP client for Short Packet Format access controllers (type 0x17).

Implements the packet format described in the manufacturer's Short Packet
Format / Extend Function documentation (driver >= 6.56):

    byte 0      : packet type             0x17  [fixed]
    byte 1      : function identifier     e.g. 0x40 / 0x94
    bytes 2-3   : reserved                0x0000
    bytes 4-7   : serial number, 32-bit little-endian integer
    bytes 8-63  : payload padded with zeros

Every request and response packet is exactly 64 bytes and is transported over
UDP port 60000.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import asdict, dataclass
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

PACKET_TYPE = 0x17
PACKET_SIZE = 64
HEADER_SIZE = 8
DEFAULT_PORT = 60000

# Function identifiers
FUNC_OPEN_DOOR = 0x40
FUNC_SEARCH = 0x94


class UhppoteError(Exception):
    """Generic controller communication error."""


class UhppoteTimeout(UhppoteError):
    """The controller did not respond within the allotted time."""


class UhppoteRefused(UhppoteError):
    """The controller responded but refused the command."""


class UhppoteProtocolError(UhppoteError):
    """Invalid or unexpected packet received."""


# --------------------------------------------------------------------------
# Packet encoding and decoding
# --------------------------------------------------------------------------


def build_packet(function: int, serial: int, payload: bytes = b"") -> bytes:
    """Build a 64-byte packet."""
    if len(payload) > PACKET_SIZE - HEADER_SIZE:
        raise ValueError("Payload is too long for a 64-byte packet")
    if not 0 <= serial <= 0xFFFFFFFF:
        raise ValueError(f"Serial number out of range: {serial}")

    packet = bytearray(PACKET_SIZE)
    packet[0] = PACKET_TYPE
    packet[1] = function
    # Bytes 2 and 3 are reserved and left as 0x00.
    struct.pack_into("<I", packet, 4, serial)
    packet[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload
    return bytes(packet)


def parse_packet(raw: bytes) -> tuple[int, int, bytes]:
    """Decode a packet and return ``(function, serial_number, payload)``."""
    if len(raw) != PACKET_SIZE:
        raise UhppoteProtocolError(
            f"Unexpected packet length: {len(raw)} bytes (expected 64)"
        )
    if raw[0] != PACKET_TYPE:
        raise UhppoteProtocolError(
            f"Unexpected packet type: 0x{raw[0]:02X} (expected 0x17)"
        )
    function = raw[1]
    serial = struct.unpack_from("<I", raw, 4)[0]
    return function, serial, raw[HEADER_SIZE:]


def _format_ip(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def _format_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _format_bcd(raw: bytes) -> str:
    return "".join(f"{b:02x}" for b in raw)


@dataclass(frozen=True)
class ControllerInfo:
    """Information returned by the discovery command (0x94)."""

    serial: int
    ip_address: str
    netmask: str
    gateway: str
    mac_address: str
    version: str
    release_date: str

    @classmethod
    def from_payload(cls, serial: int, payload: bytes) -> "ControllerInfo":
        version_bcd = _format_bcd(payload[18:20])
        date_bcd = _format_bcd(payload[20:24])
        try:
            release_date = f"{date_bcd[0:4]}-{date_bcd[4:6]}-{date_bcd[6:8]}"
        except IndexError:  # pragma: no cover - truncated packet
            release_date = date_bcd
        return cls(
            serial=serial,
            ip_address=_format_ip(payload[0:4]),
            netmask=_format_ip(payload[4:8]),
            gateway=_format_ip(payload[8:12]),
            mac_address=_format_mac(payload[12:18]),
            version=f"{int(version_bcd[0:2] or 0)}.{version_bcd[2:4]}",
            release_date=release_date,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Transport UDP
# --------------------------------------------------------------------------


class _DatagramCollector(asyncio.DatagramProtocol):
    """Minimal asyncio protocol that delegates each received datagram."""

    def __init__(self, on_datagram: Callable[[bytes, tuple[str, int]], None]) -> None:
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        _LOGGER.debug("UDP error received: %s", exc)


async def _transact(
    packet: bytes,
    address: tuple[str, int],
    *,
    timeout: float,
    broadcast: bool,
    accept: Callable[[int, int, bytes], bool],
) -> list[tuple[int, int, bytes]]:
    """Send a packet and wait for the first matching response."""
    loop = asyncio.get_running_loop()
    replies: list[tuple[int, int, bytes]] = []
    first_reply: asyncio.Future[None] = loop.create_future()

    def _on_datagram(data: bytes, addr: tuple[str, int]) -> None:
        try:
            function, serial, payload = parse_packet(data)
        except UhppoteProtocolError as err:
            _LOGGER.debug("Ignored packet from %s: %s", addr, err)
            return
        if not accept(function, serial, payload):
            _LOGGER.debug(
                "Non-matching response from %s (function 0x%02X, serial %s)",
                addr,
                function,
                serial,
            )
            return
        replies.append((function, serial, payload))
        if not first_reply.done():
            first_reply.set_result(None)

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DatagramCollector(_on_datagram),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=broadcast,
    )
    try:
        transport.sendto(packet, address)
        try:
            await asyncio.wait_for(asyncio.shield(first_reply), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        transport.close()

    return replies


async def _transact_collect(
    packet: bytes,
    address: tuple[str, int],
    *,
    timeout: float,
) -> list[tuple[int, int, bytes]]:
    """Broadcast a packet and collect all responses during ``timeout``."""
    loop = asyncio.get_running_loop()
    replies: list[tuple[int, int, bytes]] = []

    def _on_datagram(data: bytes, addr: tuple[str, int]) -> None:
        try:
            function, serial, payload = parse_packet(data)
        except UhppoteProtocolError:
            return
        if function != FUNC_SEARCH or serial == 0:
            return
        if any(serial == known for _, known, _ in replies):
            return
        replies.append((function, serial, payload))

    transport, _ = await loop.create_datagram_endpoint(
        lambda: _DatagramCollector(_on_datagram),
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        transport.sendto(packet, address)
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    return replies


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


class UhppoteController:
    """Represent a controller reachable at a given IP address."""

    def __init__(
        self,
        host: str,
        serial: int,
        *,
        port: int = DEFAULT_PORT,
        timeout: float = 2.5,
        retries: int = 2,
    ) -> None:
        self.host = host
        self.serial = int(serial)
        self.port = port
        self.timeout = timeout
        self.retries = max(0, int(retries))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UhppoteController {self.serial} @ {self.host}:{self.port}>"

    async def _request(self, function: int, payload: bytes = b"") -> bytes:
        """Send a command and return the response payload."""
        packet = build_packet(function, self.serial, payload)

        def _accept(func: int, serial: int, _payload: bytes) -> bool:
            return func == function and serial == self.serial

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                replies = await _transact(
                    packet,
                    (self.host, self.port),
                    timeout=self.timeout,
                    broadcast=False,
                    accept=_accept,
                )
            except OSError as err:
                last_error = UhppoteError(f"Network error: {err}")
                _LOGGER.debug("Attempt %s failed: %s", attempt + 1, err)
                continue

            if replies:
                return replies[0][2]

            last_error = UhppoteTimeout(
                f"No response from controller {self.serial} "
                f"({self.host}:{self.port}) for function 0x{function:02X}"
            )
            _LOGGER.debug(
                "Attempt %s/%s received no response (function 0x%02X)",
                attempt + 1,
                self.retries + 1,
                function,
            )

        assert last_error is not None
        raise last_error

    async def open_door(self, door: int) -> None:
        """Open a door remotely (function 0x40).

        Request packet: byte 8 contains the door number (1-4).
        Response packet: byte 8 is 1 if the command was accepted, otherwise 0.
        """
        if door not in (1, 2, 3, 4):
            raise ValueError(f"Invalid door number: {door} (expected 1-4)")

        payload = await self._request(FUNC_OPEN_DOOR, bytes([door]))
        if payload[0] != 0x01:
            raise UhppoteRefused(
                f"Controller {self.serial} refused to open door {door} "
                f"(return code 0x{payload[0]:02X}). Check that the door is wired "
                "correctly and that the control mode is not 'normally closed'."
            )
        _LOGGER.info("Door %s opened on controller %s", door, self.serial)


async def discover(
    *,
    broadcast_address: str = "255.255.255.255",
    port: int = DEFAULT_PORT,
    timeout: float = 3.0,
) -> list[ControllerInfo]:
    """Discover controllers on the local network (function 0x94).

    The request is broadcast with a zero serial number; each controller
    responds with its serial number, IP configuration, and version.
    """
    packet = build_packet(FUNC_SEARCH, 0)
    try:
        replies = await _transact_collect(
            packet, (broadcast_address, port), timeout=timeout
        )
    except OSError as err:
        raise UhppoteError(f"Failed to broadcast discovery request: {err}") from err

    found: list[ControllerInfo] = []
    for _function, serial, payload in replies:
        try:
            found.append(ControllerInfo.from_payload(serial, payload))
        except (IndexError, ValueError) as err:  # pragma: no cover
            _LOGGER.warning(
                "Unreadable discovery response (serial %s): %s", serial, err
            )

    found.sort(key=lambda c: c.serial)
    return found

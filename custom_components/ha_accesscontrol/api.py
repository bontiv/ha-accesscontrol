"""UDP client for Short Packet Format access controllers (type 0x17).

Implements the packet format described in the manufacturer's Short Packet
Format / Extend Function documentation (driver >= 6.56):

    byte 0      : packet type             0x17 (request/reply) or 0x19 (event)
    byte 1      : function identifier     e.g. 0x40 / 0x94
    bytes 2-3   : reserved                0x0000
    bytes 4-7   : serial number, 32-bit little-endian integer
    bytes 8-39  : payload padded with zeros
    bytes 40-43 : optional packet sequence number, echoed by the controller
    bytes 44-63 : extension added by the second revision of the protocol

Every request and response packet is exactly 64 bytes and is transported over
UDP port 60000.

This module deliberately depends on the standard library only, so that the
protocol layer can be exercised by the test suite without installing Home
Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

# Packet types. Replies to our own requests reuse the request type (0x17);
# records pushed spontaneously by the controller to the receiving server use
# 0x19 with an otherwise identical layout.
PACKET_TYPE_REQUEST = 0x17
PACKET_TYPE_EVENT = 0x19
PACKET_TYPES = (PACKET_TYPE_REQUEST, PACKET_TYPE_EVENT)

PACKET_SIZE = 64
HEADER_SIZE = 8
DATA_SIZE = 32  # payload bytes available before the sequence number
SEQUENCE_OFFSET = 40
DEFAULT_PORT = 60000

# Function identifiers
FUNC_STATUS = 0x20
FUNC_SET_TIME = 0x30
FUNC_GET_TIME = 0x32
FUNC_OPEN_DOOR = 0x40
FUNC_SET_DOOR = 0x80
FUNC_GET_DOOR = 0x82
FUNC_SET_RECEIVER = 0x90
FUNC_GET_RECEIVER = 0x92
FUNC_SEARCH = 0x94

# Door control modes (function 0x80 / 0x82)
DOOR_MODE_NORMALLY_OPEN = 1
DOOR_MODE_NORMALLY_CLOSED = 2
DOOR_MODE_CONTROLLED = 3
DOOR_MODES = (
    DOOR_MODE_NORMALLY_OPEN,
    DOOR_MODE_NORMALLY_CLOSED,
    DOOR_MODE_CONTROLLED,
)

# Record types reported in a status packet
RECORD_NONE = 0x00
RECORD_CARD = 0x01
RECORD_DOOR = 0x02
RECORD_ALARM = 0x03
RECORD_OVERWRITTEN = 0xFF


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


def build_packet(
    function: int, serial: int, payload: bytes = b"", *, sequence: int = 0
) -> bytes:
    """Build a 64-byte packet."""
    if len(payload) > PACKET_SIZE - HEADER_SIZE:
        raise ValueError("Payload is too long for a 64-byte packet")
    if sequence and len(payload) > DATA_SIZE:
        raise ValueError("Payload would overlap the sequence number field")
    if not 0 <= serial <= 0xFFFFFFFF:
        raise ValueError(f"Serial number out of range: {serial}")

    packet = bytearray(PACKET_SIZE)
    packet[0] = PACKET_TYPE_REQUEST
    packet[1] = function
    # Bytes 2 and 3 are reserved and left as 0x00.
    struct.pack_into("<I", packet, 4, serial)
    packet[HEADER_SIZE : HEADER_SIZE + len(payload)] = payload
    if sequence:
        struct.pack_into("<I", packet, SEQUENCE_OFFSET, sequence & 0xFFFFFFFF)
    return bytes(packet)


def parse_packet(raw: bytes) -> tuple[int, int, int, bytes]:
    """Decode a packet and return ``(type, function, serial_number, payload)``.

    The payload keeps its absolute layout minus the 8-byte header, so payload
    index ``n`` corresponds to byte ``n + 8`` in the documentation tables.
    """
    if len(raw) != PACKET_SIZE:
        raise UhppoteProtocolError(
            f"Unexpected packet length: {len(raw)} bytes (expected 64)"
        )
    if raw[0] not in PACKET_TYPES:
        raise UhppoteProtocolError(
            f"Unexpected packet type: 0x{raw[0]:02X} (expected 0x17 or 0x19)"
        )
    ptype = raw[0]
    function = raw[1]
    serial = struct.unpack_from("<I", raw, 4)[0]
    return ptype, function, serial, raw[HEADER_SIZE:]


def payload_sequence(payload: bytes) -> int:
    """Return the packet sequence number carried by a decoded payload."""
    offset = SEQUENCE_OFFSET - HEADER_SIZE
    if len(payload) < offset + 4:
        return 0
    return struct.unpack_from("<I", payload, offset)[0]


def _format_ip(raw: bytes) -> str:
    return ".".join(str(b) for b in raw)


def _parse_ip(value: str) -> bytes:
    parts = value.split(".")
    if len(parts) != 4:
        raise ValueError(f"Invalid IPv4 address: {value}")
    octets = [int(part) for part in parts]
    if any(not 0 <= octet <= 255 for octet in octets):
        raise ValueError(f"Invalid IPv4 address: {value}")
    return bytes(octets)


def _format_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _format_bcd(raw: bytes) -> str:
    return "".join(f"{b:02x}" for b in raw)


def bcd_to_int(value: int) -> int:
    """Decode a single BCD byte, as described in the documentation.

    The documentation gives the equivalent C expression ``x - (x / 16) * 6``.
    """
    high, low = value >> 4, value & 0x0F
    if high > 9 or low > 9:
        raise ValueError(f"Not a valid BCD byte: 0x{value:02X}")
    return high * 10 + low


def int_to_bcd(value: int) -> int:
    """Encode a value in the range 0-99 as a single BCD byte."""
    if not 0 <= value <= 99:
        raise ValueError(f"Value out of BCD range: {value}")
    return ((value // 10) << 4) | (value % 10)


def parse_bcd_datetime(raw: bytes) -> datetime | None:
    """Decode ``YY YY MM DD HH MM SS`` (7 BCD bytes) into a naive datetime.

    Returns ``None`` when the field is entirely zero, which the controller uses
    to mean "no value".
    """
    if len(raw) != 7:
        raise ValueError(f"Expected 7 BCD bytes, got {len(raw)}")
    if not any(raw):
        return None
    try:
        century, year, month, day, hour, minute, second = (
            bcd_to_int(b) for b in raw
        )
        return datetime(
            century * 100 + year, month, day, hour, minute, second
        )
    except ValueError as err:
        raise ValueError(f"Invalid BCD timestamp {raw.hex(' ')}: {err}") from err


def build_bcd_datetime(moment: datetime) -> bytes:
    """Encode a datetime as the 7 BCD bytes used by function 0x30."""
    return bytes(
        [
            int_to_bcd(moment.year // 100),
            int_to_bcd(moment.year % 100),
            int_to_bcd(moment.month),
            int_to_bcd(moment.day),
            int_to_bcd(moment.hour),
            int_to_bcd(moment.minute),
            int_to_bcd(moment.second),
        ]
    )


def doors_for_serial(serial: int) -> int:
    """Infer the number of doors from the serial number.

    The documentation states that the leading decimal digit identifies the
    model: 1 = single door bidirectional, 2 = two doors, 4 = four doors.
    """
    leading = int(str(int(serial))[0]) if serial > 0 else 0
    return {1: 1, 2: 2, 4: 4}.get(leading, 4)


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

    @property
    def doors(self) -> int:
        """Number of doors inferred from the serial number."""
        return doors_for_serial(self.serial)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControllerStatus:
    """Controller state returned by function 0x20.

    The same layout is pushed spontaneously as a 0x19 packet once a receiving
    server has been configured with function 0x90.
    """

    serial: int
    last_record_index: int
    record_type: int
    granted: bool
    door: int
    direction: int
    card_number: int
    event_time: datetime | None
    reason_code: int
    door_sensors: tuple[bool, ...]
    buttons: tuple[bool, ...]
    error_code: int
    relays: tuple[bool, ...]
    forced_lock: bool
    fire: bool
    controller_time: datetime | None
    sequence: int = 0

    @classmethod
    def from_payload(cls, serial: int, payload: bytes) -> "ControllerStatus":
        """Decode the 56-byte payload of a status packet.

        Payload index ``n`` is byte ``n + 8`` of the documentation tables.
        """
        if len(payload) < 46:
            raise UhppoteProtocolError(
                f"Status payload too short: {len(payload)} bytes"
            )

        try:
            event_time = parse_bcd_datetime(payload[12:19])
        except ValueError as err:
            _LOGGER.debug("Ignoring unreadable event timestamp: %s", err)
            event_time = None

        # Bytes 51-53 (the controller's current date) were added by a later
        # firmware revision; older controllers leave them at zero and only
        # report the time of day in bytes 37-39.
        controller_time: datetime | None = None
        if any(payload[43:46]):
            try:
                controller_time = datetime(
                    2000 + bcd_to_int(payload[43]),
                    bcd_to_int(payload[44]),
                    bcd_to_int(payload[45]),
                    bcd_to_int(payload[29]),
                    bcd_to_int(payload[30]),
                    bcd_to_int(payload[31]),
                )
            except ValueError as err:
                _LOGGER.debug("Ignoring unreadable controller clock: %s", err)

        relay_bits = payload[41]
        flags = payload[42]

        return cls(
            serial=serial,
            last_record_index=struct.unpack_from("<I", payload, 0)[0],
            record_type=payload[4],
            granted=payload[5] == 1,
            door=payload[6],
            direction=payload[7],
            card_number=struct.unpack_from("<I", payload, 8)[0],
            event_time=event_time,
            reason_code=payload[19],
            door_sensors=tuple(payload[20 + i] == 1 for i in range(4)),
            buttons=tuple(payload[24 + i] == 1 for i in range(4)),
            error_code=payload[28],
            relays=tuple(bool(relay_bits & (1 << i)) for i in range(4)),
            forced_lock=bool(flags & 0x01),
            fire=bool(flags & 0x02),
            controller_time=controller_time,
            sequence=payload_sequence(payload),
        )

    @property
    def has_event(self) -> bool:
        """Whether the packet carries an actual record."""
        return self.record_type not in (RECORD_NONE, RECORD_OVERWRITTEN)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["event_time"] = (
            self.event_time.isoformat() if self.event_time else None
        )
        data["controller_time"] = (
            self.controller_time.isoformat() if self.controller_time else None
        )
        data["door_sensors"] = list(self.door_sensors)
        data["buttons"] = list(self.buttons)
        data["relays"] = list(self.relays)
        return data


@dataclass(frozen=True)
class DoorConfig:
    """Door control parameters exchanged by functions 0x80 and 0x82."""

    door: int
    mode: int
    delay: int

    @classmethod
    def from_payload(cls, payload: bytes) -> "DoorConfig":
        if len(payload) < 3:
            raise UhppoteProtocolError("Door configuration payload too short")
        return cls(door=payload[0], mode=payload[1], delay=payload[2])

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReceiverConfig:
    """Receiving-server parameters exchanged by functions 0x90 and 0x92."""

    ip_address: str
    port: int
    interval: int

    @classmethod
    def from_payload(cls, payload: bytes) -> "ReceiverConfig":
        if len(payload) < 7:
            raise UhppoteProtocolError("Receiver configuration payload too short")
        return cls(
            ip_address=_format_ip(payload[0:4]),
            port=struct.unpack_from("<H", payload, 4)[0],
            interval=payload[6],
        )

    @property
    def enabled(self) -> bool:
        """Whether the controller is configured to push records anywhere."""
        return self.ip_address != "0.0.0.0"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# UDP transport
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
    accept: Callable[[int, int, int, bytes], bool],
) -> list[tuple[int, int, int, bytes]]:
    """Send a packet and wait for the first matching response."""
    loop = asyncio.get_running_loop()
    replies: list[tuple[int, int, int, bytes]] = []
    first_reply: asyncio.Future[None] = loop.create_future()

    def _on_datagram(data: bytes, addr: tuple[str, int]) -> None:
        try:
            ptype, function, serial, payload = parse_packet(data)
        except UhppoteProtocolError as err:
            _LOGGER.debug("Ignored packet from %s: %s", addr, err)
            return
        if not accept(ptype, function, serial, payload):
            _LOGGER.debug(
                "Non-matching response from %s (function 0x%02X, serial %s)",
                addr,
                function,
                serial,
            )
            return
        replies.append((ptype, function, serial, payload))
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
) -> list[tuple[int, int, int, bytes]]:
    """Broadcast a packet and collect all responses during ``timeout``."""
    loop = asyncio.get_running_loop()
    replies: list[tuple[int, int, int, bytes]] = []

    def _on_datagram(data: bytes, addr: tuple[str, int]) -> None:
        try:
            ptype, function, serial, payload = parse_packet(data)
        except UhppoteProtocolError:
            return
        if function != FUNC_SEARCH or serial == 0:
            return
        if any(serial == known for _, _, known, _ in replies):
            return
        replies.append((ptype, function, serial, payload))

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


def local_ip_for(host: str, port: int = DEFAULT_PORT) -> str | None:
    """Return the local address the kernel would use to reach ``host``.

    Connecting a UDP socket sends no traffic; it only asks the routing table
    which interface would be used. This is more reliable than the address
    Home Assistant advertises on a multi-homed host.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, port))
        return sock.getsockname()[0]
    except OSError as err:
        _LOGGER.debug("Unable to determine the local address for %s: %s", host, err)
        return None
    finally:
        sock.close()


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
        self._sequence = 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UhppoteController {self.serial} @ {self.host}:{self.port}>"

    @property
    def doors(self) -> int:
        """Number of doors inferred from the serial number."""
        return doors_for_serial(self.serial)

    def _next_sequence(self) -> int:
        # Wrap before 0xFFFFFFFF and skip 0, which means "no sequence".
        self._sequence = (self._sequence % 0xFFFFFFFE) + 1
        return self._sequence

    async def _request(self, function: int, payload: bytes = b"") -> bytes:
        """Send a command and return the response payload."""
        sequence = self._next_sequence()
        packet = build_packet(function, self.serial, payload, sequence=sequence)

        def _accept(ptype: int, func: int, serial: int, reply: bytes) -> bool:
            if func != function or serial != self.serial:
                return False
            # Firmware that does not echo the sequence number replies with 0;
            # accept it rather than time out, but reject stale sequences.
            echoed = payload_sequence(reply)
            return echoed in (0, sequence)

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
                return replies[0][3]

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

    def _check_door(self, door: int) -> None:
        if door not in (1, 2, 3, 4):
            raise ValueError(f"Invalid door number: {door} (expected 1-4)")

    async def open_door(self, door: int) -> None:
        """Open a door remotely (function 0x40).

        Request packet: byte 8 contains the door number (1-4).
        Response packet: byte 8 is 1 if the command was accepted, otherwise 0.
        """
        self._check_door(door)

        payload = await self._request(FUNC_OPEN_DOOR, bytes([door]))
        if payload[0] != 0x01:
            raise UhppoteRefused(
                f"Controller {self.serial} refused to open door {door} "
                f"(return code 0x{payload[0]:02X}). Check that the door is wired "
                "correctly and that the control mode is not 'normally closed'."
            )
        _LOGGER.info("Door %s opened on controller %s", door, self.serial)

    async def get_status(self) -> ControllerStatus:
        """Read the full controller state (function 0x20)."""
        payload = await self._request(FUNC_STATUS)
        return ControllerStatus.from_payload(self.serial, payload)

    async def get_time(self) -> datetime | None:
        """Read the controller clock (function 0x32)."""
        payload = await self._request(FUNC_GET_TIME)
        return parse_bcd_datetime(payload[0:7])

    async def set_time(self, moment: datetime) -> datetime | None:
        """Set the controller clock (function 0x30), which echoes the value."""
        payload = await self._request(FUNC_SET_TIME, build_bcd_datetime(moment))
        return parse_bcd_datetime(payload[0:7])

    async def get_door_config(self, door: int) -> DoorConfig:
        """Read a door's control mode and open delay (function 0x82)."""
        self._check_door(door)
        payload = await self._request(FUNC_GET_DOOR, bytes([door]))
        config = DoorConfig.from_payload(payload)
        if config.door != door:
            raise UhppoteProtocolError(
                f"Controller {self.serial} answered for door {config.door} "
                f"instead of door {door}"
            )
        return config

    async def set_door_config(self, door: int, mode: int, delay: int) -> DoorConfig:
        """Set a door's control mode and open delay (function 0x80).

        Both fields travel in the same packet, so the caller must supply the
        delay it wants to keep; sending a stale value overwrites the one stored
        in the controller.
        """
        self._check_door(door)
        if mode not in DOOR_MODES:
            raise ValueError(f"Invalid door mode: {mode} (expected 1, 2 or 3)")
        if not 0 <= delay <= 255:
            raise ValueError(f"Invalid open delay: {delay} (expected 0-255)")

        payload = await self._request(
            FUNC_SET_DOOR, bytes([door, mode, delay])
        )
        config = DoorConfig.from_payload(payload)
        if config.door == 0:
            raise UhppoteRefused(
                f"Controller {self.serial} refused the configuration of door {door}"
            )
        return config

    async def get_receiver(self) -> ReceiverConfig:
        """Read the receiving-server configuration (function 0x92)."""
        payload = await self._request(FUNC_GET_RECEIVER)
        return ReceiverConfig.from_payload(payload)

    async def set_receiver(
        self, ip_address: str, port: int, interval: int = 0
    ) -> None:
        """Point the controller at a receiving server (function 0x90).

        ``interval`` is a heartbeat in seconds; 0 means "only send a packet
        when a new record is created". Setting ``ip_address`` to 0.0.0.0
        disables pushing altogether.
        """
        if not 0 <= port <= 0xFFFF:
            raise ValueError(f"Invalid port: {port}")
        if not 0 <= interval <= 255:
            raise ValueError(f"Invalid interval: {interval} (expected 0-255)")

        payload = _parse_ip(ip_address) + struct.pack("<H", port) + bytes([interval])
        reply = await self._request(FUNC_SET_RECEIVER, payload)
        if reply[0] != 0x01:
            raise UhppoteRefused(
                f"Controller {self.serial} refused the receiving-server "
                f"configuration (return code 0x{reply[0]:02X})"
            )


# --------------------------------------------------------------------------
# Event listener
# --------------------------------------------------------------------------


@dataclass
class EventListener:
    """UDP server receiving records pushed by controllers (function 0x90).

    A controller configured with a receiving server sends a status packet
    whenever a record is created, and optionally on a fixed heartbeat. Those
    packets carry type 0x19 but are otherwise identical to a 0x20 reply.

    The listener is deliberately permissive about the packet type and strict
    about everything else: a 64-byte packet, a known type, function 0x20, and
    a serial number that has been registered by a caller.
    """

    port: int
    callbacks: dict[int, Callable[[ControllerStatus], None]] = field(
        default_factory=dict
    )
    _transport: asyncio.DatagramTransport | None = None

    @property
    def running(self) -> bool:
        return self._transport is not None

    @property
    def bound_port(self) -> int | None:
        """Actual port in use, which differs from ``port`` when it is 0."""
        if self._transport is None:
            return None
        return self._transport.get_extra_info("sockname")[1]

    def register(
        self, serial: int, callback: Callable[[ControllerStatus], None]
    ) -> None:
        """Route packets from ``serial`` to ``callback``."""
        self.callbacks[int(serial)] = callback

    def unregister(self, serial: int) -> None:
        self.callbacks.pop(int(serial), None)

    async def async_start(self) -> None:
        """Bind the listening socket."""
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DatagramCollector(self._on_datagram),
                local_addr=("0.0.0.0", self.port),
            )
        except OSError as err:
            raise UhppoteError(
                f"Unable to listen for controller events on UDP port "
                f"{self.port}: {err}"
            ) from err
        self._transport = transport
        _LOGGER.debug("Listening for controller events on UDP port %s", self.port)

    async def async_stop(self) -> None:
        """Close the listening socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _on_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            ptype, function, serial, payload = parse_packet(data)
        except UhppoteProtocolError as err:
            _LOGGER.debug("Ignored event packet from %s: %s", addr, err)
            return

        if function != FUNC_STATUS:
            _LOGGER.debug(
                "Ignored event packet from %s with function 0x%02X", addr, function
            )
            return

        callback = self.callbacks.get(serial)
        if callback is None:
            _LOGGER.debug(
                "Ignored event from unknown controller %s at %s", serial, addr
            )
            return

        try:
            status = ControllerStatus.from_payload(serial, payload)
        except (UhppoteProtocolError, ValueError) as err:
            _LOGGER.warning("Unreadable event from controller %s: %s", serial, err)
            return

        if ptype != PACKET_TYPE_EVENT:
            # The documentation describes pushed records as type 0x19; accept
            # 0x17 too, since only the prose mentions the distinction.
            _LOGGER.debug(
                "Controller %s pushed a record with type 0x%02X", serial, ptype
            )

        callback(status)


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
    for _ptype, _function, serial, payload in replies:
        try:
            found.append(ControllerInfo.from_payload(serial, payload))
        except (IndexError, ValueError) as err:  # pragma: no cover
            _LOGGER.warning(
                "Unreadable discovery response (serial %s): %s", serial, err
            )

    found.sort(key=lambda c: c.serial)
    return found

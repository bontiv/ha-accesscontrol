"""Fixtures for the protocol tests.

``api.py`` is loaded straight from its path and registered as the top-level
module ``uhppote_api``. Importing the integration package instead would
execute ``custom_components/ha_accesscontrol/__init__.py``, which pulls in
Home Assistant; the protocol layer is deliberately free of that dependency
so these tests run anywhere.
"""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

_COMPONENT = (
    Path(__file__).resolve().parents[2] / "custom_components" / "ha_accesscontrol"
)


def _load(module: str, alias: str):
    """Import one module of the integration without its package."""
    spec = importlib.util.spec_from_file_location(alias, _COMPONENT / f"{module}.py")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[alias] = loaded
    spec.loader.exec_module(loaded)
    return loaded


api = _load("api", "uhppote_api")
timezones = _load("timezones", "uhppote_timezones")


SERIAL = 223000123  # 0x0D4AB63B, the documentation example
DOC_SERIAL = 229999901  # 0x0DB5851D, the serial used by the captured packets


def hexpkt(text: str) -> bytes:
    """Decode a packet written the way the documentation prints it."""
    raw = bytes.fromhex(text.replace("\n", " "))
    assert len(raw) == 64, f"expected 64 bytes, got {len(raw)}"
    return raw


class FakeController(asyncio.DatagramProtocol):
    """Respond like a real controller on an ephemeral port."""

    def __init__(
        self,
        serial: int,
        *,
        grant: bool = True,
        silent: bool = False,
        echo_sequence: bool = True,
        force_sequence: int | None = None,
    ) -> None:
        self.serial = serial
        self.grant = grant
        self.silent = silent
        self.echo_sequence = echo_sequence
        self.force_sequence = force_sequence
        self.received: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

        # Mutable controller state the tests can inspect and preset.
        self.door_config: dict[int, tuple[int, int]] = {
            door: (api.DOOR_MODE_CONTROLLED, 3) for door in (1, 2, 3, 4)
        }
        self.receiver: tuple[str, int, int] = ("0.0.0.0", 0, 0)
        self.status_payload: bytes | None = None
        self.clock: bytes = bytes([0x20, 0x15, 0x04, 0x29, 0x16, 0x48, 0x00])

    @property
    def port(self) -> int:
        return self.transport.get_extra_info("sockname")[1]

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.append(data)
        if self.silent:
            return

        _ptype, func, _serial, payload = api.parse_packet(data)
        reply = bytearray(64)
        reply[0] = api.PACKET_TYPE_REQUEST
        reply[1] = func
        struct.pack_into("<I", reply, 4, self.serial)

        if func == api.FUNC_OPEN_DOOR:
            reply[8] = 0x01 if self.grant else 0x00
        elif func == api.FUNC_SEARCH:
            reply[8:12] = bytes([127, 0, 0, 1])
            reply[12:16] = bytes([255, 255, 255, 0])
            reply[16:20] = bytes([127, 0, 0, 1])
            reply[20:26] = bytes([0x00, 0x66, 0x19, 0x39, 0x55, 0x2D])
            reply[26:28] = bytes([0x06, 0x56])
            reply[28:32] = bytes([0x20, 0x19, 0x08, 0x29])
        elif func == api.FUNC_STATUS:
            body = self.status_payload or bytes(56)
            reply[8 : 8 + len(body)] = body[:56]
        elif func == api.FUNC_GET_DOOR:
            door = payload[0]
            mode, delay = self.door_config[door]
            reply[8:11] = bytes([door, mode, delay])
        elif func == api.FUNC_SET_DOOR:
            door, mode, delay = payload[0], payload[1], payload[2]
            if door in self.door_config:
                self.door_config[door] = (mode, delay)
                reply[8:11] = bytes([door, mode, delay])
        elif func == api.FUNC_GET_RECEIVER:
            ip, port, interval = self.receiver
            reply[8:12] = bytes(int(p) for p in ip.split("."))
            struct.pack_into("<H", reply, 12, port)
            reply[14] = interval
        elif func == api.FUNC_SET_RECEIVER:
            self.receiver = (
                ".".join(str(b) for b in payload[0:4]),
                struct.unpack_from("<H", payload, 4)[0],
                payload[6],
            )
            reply[8] = 0x01
        elif func == api.FUNC_GET_TIME:
            reply[8:15] = self.clock
        elif func == api.FUNC_SET_TIME:
            self.clock = bytes(payload[0:7])
            reply[8:15] = self.clock

        # A status payload spans the sequence number field, so write it last.
        if self.force_sequence is not None:
            struct.pack_into("<I", reply, api.SEQUENCE_OFFSET, self.force_sequence)
        elif self.echo_sequence:
            struct.pack_into(
                "<I", reply, api.SEQUENCE_OFFSET, api.payload_sequence(payload)
            )

        self.transport.sendto(bytes(reply), addr)


@pytest.fixture
async def fake_controller_factory() -> AsyncIterator[Callable[..., object]]:
    """Return a factory starting fake controllers, all closed on teardown.

    The fixture is async so that teardown runs while the event loop is still
    alive; closing a transport afterwards leaks the socket.
    """
    started: list[asyncio.DatagramTransport] = []

    async def _start(serial: int = SERIAL, **kwargs) -> FakeController:
        loop = asyncio.get_running_loop()
        proto = FakeController(serial, **kwargs)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: proto, local_addr=("127.0.0.1", 0)
        )
        started.append(transport)
        return proto

    yield _start

    for transport in started:
        transport.close()


@pytest.fixture
async def fake_controller(fake_controller_factory) -> FakeController:
    """A fake controller answering every implemented function."""
    return await fake_controller_factory()


@pytest.fixture
def connect() -> Callable[..., object]:
    """Return a helper building a client pointed at a fake controller."""

    def _connect(controller: FakeController, **kwargs) -> object:
        kwargs.setdefault("timeout", 1.0)
        return api.UhppoteController(
            "127.0.0.1", controller.serial, port=controller.port, **kwargs
        )

    return _connect


@pytest.fixture
async def listener() -> AsyncIterator[object]:
    """A push listener bound to an ephemeral port, stopped on teardown."""
    instance = api.EventListener(0)
    await instance.async_start()
    try:
        yield instance
    finally:
        await instance.async_stop()


@pytest.fixture
def send_datagram() -> Callable[..., object]:
    """Return a coroutine sending one datagram to a local UDP port."""

    async def _send(port: int, packet: bytes, *, settle: float = 0.15) -> None:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, remote_addr=("127.0.0.1", port)
        )
        try:
            transport.sendto(packet)
            await asyncio.sleep(settle)
        finally:
            transport.close()

    return _send


@pytest.fixture(autouse=True)
def allow_real_sockets():
    """Let these tests use loopback UDP.

    pytest-socket comes with the integration group and blocks sockets for the
    whole session. The protocol tests exercise a real datagram exchange, which
    is the point of them, so re-enable sockets for this package only.
    """
    try:
        import pytest_socket
    except ImportError:
        return
    pytest_socket.enable_socket()

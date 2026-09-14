"""The UDP listener receiving records pushed by controllers (function 0x90)."""

from __future__ import annotations

import pytest
import uhppote_api as api
from .captures import STATUS_RECENT
from .conftest import DOC_SERIAL


def push_packet(
    serial: int = DOC_SERIAL,
    *,
    ptype: int = api.PACKET_TYPE_EVENT,
    function: int = api.FUNC_STATUS,
) -> bytes:
    """Build a record as a controller would push it."""
    raw = bytearray(STATUS_RECENT)
    raw[0] = ptype
    raw[1] = function
    raw[4:8] = int(serial).to_bytes(4, "little")
    return bytes(raw)


@pytest.mark.parametrize(
    ("ptype", "label"),
    [
        (api.PACKET_TYPE_EVENT, "0x19, as the documentation describes"),
        (api.PACKET_TYPE_REQUEST, "0x17, since only prose documents 0x19"),
    ],
)
async def test_dispatches_pushed_record(
    listener, send_datagram, ptype: int, label: str
) -> None:
    received: list[api.ControllerStatus] = []
    listener.register(DOC_SERIAL, received.append)

    await send_datagram(listener.bound_port, push_packet(ptype=ptype))

    assert len(received) == 1, f"a record of type {label} should be dispatched"
    assert received[0].serial == DOC_SERIAL
    assert received[0].card_number == 3659533


@pytest.mark.parametrize(
    ("packet", "reason"),
    [
        (push_packet(123456789), "unknown serial number"),
        (push_packet(function=api.FUNC_OPEN_DOOR), "not a status packet"),
        (b"garbage", "not 64 bytes"),
        (bytes([0x18]) + bytes(63), "unknown packet type"),
    ],
)
async def test_filters_unwanted_packets(
    listener, send_datagram, packet: bytes, reason: str
) -> None:
    received: list[api.ControllerStatus] = []
    listener.register(DOC_SERIAL, received.append)

    await send_datagram(listener.bound_port, packet)

    assert received == [], f"a packet with {reason} must be dropped"


async def test_truncated_status_is_reported_not_raised(
    listener, send_datagram
) -> None:
    """A well-framed packet with an unreadable body must not kill the socket."""
    received: list[api.ControllerStatus] = []
    listener.register(DOC_SERIAL, received.append)
    corrupt = bytearray(push_packet())
    corrupt[20:27] = bytes([0xFF] * 7)  # invalid BCD timestamp

    await send_datagram(listener.bound_port, bytes(corrupt))
    await send_datagram(listener.bound_port, push_packet())

    assert len(received) == 2, "the listener must keep serving after a bad packet"
    assert received[0].event_time is None


async def test_unregister_stops_delivery(listener, send_datagram) -> None:
    received: list[api.ControllerStatus] = []
    listener.register(DOC_SERIAL, received.append)
    listener.unregister(DOC_SERIAL)

    await send_datagram(listener.bound_port, push_packet())

    assert received == []


async def test_routes_each_serial_to_its_own_callback(
    listener, send_datagram
) -> None:
    first: list[api.ControllerStatus] = []
    second: list[api.ControllerStatus] = []
    listener.register(DOC_SERIAL, first.append)
    listener.register(223000123, second.append)

    await send_datagram(listener.bound_port, push_packet(DOC_SERIAL))
    await send_datagram(listener.bound_port, push_packet(223000123))

    assert [s.serial for s in first] == [DOC_SERIAL]
    assert [s.serial for s in second] == [223000123]


async def test_start_is_idempotent_and_reports_state() -> None:
    instance = api.EventListener(0)
    assert instance.running is False
    assert instance.bound_port is None

    await instance.async_start()
    try:
        port = instance.bound_port
        assert instance.running is True

        await instance.async_start()  # second call is a no-op

        assert instance.bound_port == port
    finally:
        await instance.async_stop()

    assert instance.running is False


async def test_stop_is_idempotent(listener) -> None:
    await listener.async_stop()
    await listener.async_stop()

    assert listener.running is False


async def test_port_already_in_use_raises(listener) -> None:
    clash = api.EventListener(listener.bound_port)

    with pytest.raises(api.UhppoteError):
        await clash.async_start()

"""Network exchanges against a simulated controller."""

from __future__ import annotations

from datetime import datetime

import pytest
import uhppote_api as api
from captures import STATUS_RECENT
from conftest import DOC_SERIAL, SERIAL

# --------------------------------------------------------------- open door


async def test_open_door_success(fake_controller, connect) -> None:
    await connect(fake_controller).open_door(2)

    assert len(fake_controller.received) == 1
    assert fake_controller.received[0][1] == api.FUNC_OPEN_DOOR
    assert fake_controller.received[0][8] == 2


async def test_open_door_refused(fake_controller_factory, connect) -> None:
    controller = await fake_controller_factory(grant=False)

    with pytest.raises(api.UhppoteRefused):
        await connect(controller).open_door(1)


@pytest.mark.parametrize("door", [0, 5, -1])
async def test_open_door_rejects_invalid_door(door: int) -> None:
    client = api.UhppoteController("127.0.0.1", SERIAL)

    with pytest.raises(ValueError):
        await client.open_door(door)


# ------------------------------------------------------ retries, filtering


async def test_timeout_retries_then_raises(fake_controller_factory, connect) -> None:
    controller = await fake_controller_factory(silent=True)

    with pytest.raises(api.UhppoteTimeout):
        await connect(controller, timeout=0.3, retries=2).open_door(1)

    assert len(controller.received) == 3, "one initial attempt plus two retries"


async def test_reply_from_another_controller_is_ignored(
    fake_controller, connect
) -> None:
    client = connect(fake_controller, timeout=0.3, retries=0)
    fake_controller.serial = 999999  # answer with a different serial number

    with pytest.raises(api.UhppoteTimeout):
        await client.open_door(1)


async def test_stale_sequence_is_ignored(fake_controller_factory, connect) -> None:
    """A reply echoing another request's sequence number must be dropped."""
    controller = await fake_controller_factory(force_sequence=0xDEADBEEF)

    with pytest.raises(api.UhppoteTimeout):
        await connect(controller, timeout=0.3, retries=0).open_door(1)


async def test_zero_sequence_is_accepted(fake_controller_factory, connect) -> None:
    """Firmware that does not echo the sequence number must still work."""
    controller = await fake_controller_factory(echo_sequence=False)

    await connect(controller).open_door(1)


async def test_sequence_increments_between_requests(fake_controller, connect) -> None:
    client = connect(fake_controller)

    await client.open_door(1)
    await client.open_door(1)

    sequences = [api.payload_sequence(pkt[8:]) for pkt in fake_controller.received]
    assert sequences[1] == sequences[0] + 1
    assert 0 not in sequences, "0 means 'no sequence' and must be skipped"


# -------------------------------------------------------------- discovery


async def test_discover_roundtrip(fake_controller) -> None:
    found = await api.discover(
        broadcast_address="127.0.0.1", port=fake_controller.port, timeout=0.6
    )

    assert fake_controller.received[0][1] == api.FUNC_SEARCH
    assert fake_controller.received[0][4:8] == bytes(4), (
        "the request must carry a zero serial number"
    )
    assert [c.serial for c in found] == [SERIAL]
    assert found[0].ip_address == "127.0.0.1"
    assert found[0].version == "6.56"


# ----------------------------------------------------------------- status


async def test_get_status(fake_controller_factory, connect) -> None:
    controller = await fake_controller_factory(DOC_SERIAL)
    controller.status_payload = STATUS_RECENT[8:]

    status = await connect(controller).get_status()

    assert controller.received[0][1] == api.FUNC_STATUS
    assert status.card_number == 3659533
    assert status.door_sensors[0] is True


# ----------------------------------------------------------- door control


async def test_door_config_roundtrip(fake_controller, connect) -> None:
    client = connect(fake_controller)

    assert await client.get_door_config(2) == api.DoorConfig(
        door=2, mode=api.DOOR_MODE_CONTROLLED, delay=3
    )

    updated = await client.set_door_config(2, api.DOOR_MODE_NORMALLY_OPEN, 5)

    assert updated == api.DoorConfig(door=2, mode=api.DOOR_MODE_NORMALLY_OPEN, delay=5)
    assert fake_controller.door_config[2] == (api.DOOR_MODE_NORMALLY_OPEN, 5)
    assert fake_controller.door_config[1] == (api.DOOR_MODE_CONTROLLED, 3), (
        "other doors must be left untouched"
    )


@pytest.mark.parametrize(
    ("door", "mode", "delay", "reason"),
    [
        (0, 3, 3, "door 0"),
        (5, 3, 3, "door 5"),
        (1, 0, 3, "mode 0"),
        (1, 4, 3, "mode 4"),
        (1, 3, 256, "delay 256"),
        (1, 3, -1, "delay -1"),
    ],
)
async def test_set_door_config_validates_arguments(
    door: int, mode: int, delay: int, reason: str
) -> None:
    client = api.UhppoteController("127.0.0.1", SERIAL)

    with pytest.raises(ValueError):
        await client.set_door_config(door, mode, delay)


async def test_set_door_config_refusal(fake_controller, connect) -> None:
    """The controller reports a failure by returning door number 0."""
    fake_controller.door_config.pop(4)  # a door this model does not have

    with pytest.raises(api.UhppoteRefused):
        await connect(fake_controller).set_door_config(4, api.DOOR_MODE_NORMALLY_OPEN, 3)


async def test_get_door_config_detects_mismatched_answer(
    fake_controller, connect
) -> None:
    client = connect(fake_controller)
    original = fake_controller.datagram_received

    def _swap_door(data: bytes, addr) -> None:
        patched = bytearray(data)
        patched[8] = 2 if patched[8] == 1 else 1
        original(bytes(patched), addr)

    fake_controller.datagram_received = _swap_door

    with pytest.raises(api.UhppoteProtocolError):
        await client.get_door_config(1)


# -------------------------------------------------------------- receiver


async def test_receiver_roundtrip(fake_controller, connect) -> None:
    client = connect(fake_controller)

    assert (await client.get_receiver()).enabled is False

    await client.set_receiver("192.168.168.101", 61005, 5)

    assert fake_controller.receiver == ("192.168.168.101", 61005, 5)
    current = await client.get_receiver()
    assert current == api.ReceiverConfig(
        ip_address="192.168.168.101", port=61005, interval=5
    )
    assert current.enabled is True


@pytest.mark.parametrize(
    ("ip", "port", "interval", "reason"),
    [
        ("192.168.1", 9001, 0, "short address"),
        ("192.168.1.300", 9001, 0, "octet out of range"),
        ("192.168.1.1", 70000, 0, "port out of range"),
        ("192.168.1.1", -1, 0, "negative port"),
        ("192.168.1.1", 9001, 256, "interval out of range"),
    ],
)
async def test_set_receiver_validates_arguments(
    ip: str, port: int, interval: int, reason: str
) -> None:
    client = api.UhppoteController("127.0.0.1", SERIAL)

    with pytest.raises(ValueError):
        await client.set_receiver(ip, port, interval)


# ----------------------------------------------------------------- clock


async def test_clock_roundtrip(fake_controller, connect) -> None:
    client = connect(fake_controller)

    assert await client.get_time() == datetime(2015, 4, 29, 16, 48, 0)

    moment = datetime(2024, 12, 31, 23, 59, 58)
    assert await client.set_time(moment) == moment
    assert await client.get_time() == moment
